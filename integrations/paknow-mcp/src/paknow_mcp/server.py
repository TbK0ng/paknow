"""PAknow MCP server.

Capability boundary:
- ``paknow_ask`` is the default and owns grounded answer generation.
- ``paknow_search`` / ``paknow_evidence`` are read-only inspection tools.
- Image tools return the original page/figure bytes.
- No market data, backtesting, numeric prediction, or writes.
"""

from __future__ import annotations

import base64
import binascii
import mimetypes
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import Field

from . import __version__
from .client import ChartImage, PaknowClient, PaknowError
from .config import Settings, resolve_image_path
from .formatting import (
    display_answer,
    render_ask,
    render_documents,
    render_evidence,
    render_health,
    render_image,
    render_run,
    render_search,
    render_sops,
    render_source,
)
from .models import (
    AskOutput,
    Citation,
    DocumentInfo,
    DocumentsOutput,
    EvidenceOutput,
    FigureRef,
    HealthOutput,
    ImageOutput,
    RunOutput,
    SearchHit,
    SearchOutput,
    SOPInfo,
    SOPsOutput,
    SourceOutput,
)

Question = Annotated[
    str,
    Field(
        min_length=1,
        max_length=8000,
        description=(
            "A standalone user question. Resolve pronouns and prior-turn references "
            "before calling."
        ),
    ),
]
TopK = Annotated[
    int,
    Field(ge=1, le=20, description="Number of compact evidence hits to return (1-20)."),
]
Cutoff = Annotated[
    float,
    Field(
        ge=0.0,
        le=1.0,
        description=(
            "Optional 0..1 right-edge mask for chart images. Bars beyond the mask "
            "are never sent to the model."
        ),
    ),
]
Scale = Annotated[
    float,
    Field(
        ge=0.5,
        le=4.0,
        description="PDF render scale (0.5-4.0; 1.5 is a good default).",
    ),
]
SOPStatus = Literal[
    "observed",
    "candidate",
    "replay_validated",
    "shadow",
    "active",
    "superseded",
    "retired",
]

_INSTRUCTIONS = """PAknow is a local, evidence-first price-action knowledge base built from
trading books. It answers with citations that map to book/chapter/figure/PDF page.

Routing rules:
1. For any price-action definition, rule, mechanism, condition, comparison, or chart
   question, call paknow_ask first. Never answer such questions from model memory.
2. Call paknow_search only to fill a stated evidence gap, inspect multiple mentions,
   or support a multi-hop comparison. It returns retrieval hits, not an answer.
3. Call paknow_evidence to verify a quote or read the full evidence unit. Call
   paknow_page_image or paknow_figure_image only when visual verification is needed.
4. Rewrite follow-up questions into standalone questions before calling paknow_ask;
   PAknow is stateless between calls.
5. Preserve the returned citation mapping. Do not invent page numbers, figure labels,
   quotes, or source titles. The tool text uses 1-based display indices; the structured
   result keeps the backend's 0-based indices.
6. If PAknow reports insufficient evidence, say so. Do not fill gaps with model
   knowledge. If PAknow is unavailable, report the outage.
7. Out of scope: live market data, backtesting, numeric predictions, trading advice,
   file edits, and web search."""

_TOOL_ASK = """Answer a price-action knowledge question with a grounded, citation-backed
response from PAknow.

Use when the user asks what a pattern/rule/mechanism is, when it applies, how it differs
from a related concept, or asks about an uploaded chart. This calls PAknow's full
route→retrieve→verify→generate pipeline, so expect roughly 20-60 seconds (longer on the
first request while local models load).

Do not use for general chat, live prices, backtests, or numeric forecasts. Always pass a
standalone question; resolve pronouns from the conversation first. The response includes
an answer, uncertainty, citations with evidence IDs, and cited figures. Do not rewrite the
citation list or invent locators. Set include_figures=true only when the user asks to see
the cited charts; at most four images are attached."""

_TOOL_CHART = """Analyze an uploaded price chart with PAknow's vision + retrieval pipeline.

Provide exactly one of image_path (server-local file) or image_base64 (data URI or raw
base64). cutoff optionally masks the rightmost fraction (0..1) before the image reaches
the model, preventing look-ahead leakage. Returns the same answer, uncertainty, citations,
and figures as paknow_ask. Use only for chart analysis, not for text-only questions."""

_TOOL_SEARCH = """Retrieval-only search over PAknow's evidence index. Returns compact evidence
hits with evidence IDs, locators, scores, and linked figure IDs; it does not generate an
answer.

Use to inspect evidence after paknow_ask reports a gap, to find where a term is discussed,
to compare multiple passages, or to gather evidence for a multi-hop answer. Optional
doc_id and block_type filters narrow the index. Do not use this as a substitute for
paknow_ask, and do not present raw hits as a final grounded answer without citing them."""

_TOOL_EVIDENCE = """Read one full evidence unit by ID, including its source locator and linked
figures.

Use to verify a quotation, inspect adjacent context, or resolve an evidence ID returned by
paknow_ask/paknow_search. This is read-only and does not run retrieval or generation."""

_TOOL_DOCUMENTS = """List the documents in the PAknow corpus with title, author, language,
format, page count, and evidence-unit count.

Use for questions such as “what books are in the knowledge base?” or to choose a doc_id
for paknow_search filters. Do not use for content questions."""

_TOOL_SOURCE = """Return metadata and counts for one PAknow document by doc_id.

Use after paknow_documents or when the user asks about a specific book's edition, page
count, or indexed size."""

_TOOL_PAGE_IMAGE = """Render a PDF page to PNG and return it as an image.

Use only when the answer requires visual verification of a page or figure. doc_id and page
are 1-based. Keep scale modest (1.5 default) to avoid oversized images."""

_TOOL_FIGURE_IMAGE = """Return a stored figure asset by figure_id as an image.

Use when the user asks to see a cited figure, or when visual inspection of a figure is
needed. Prefer this over reconstructing a path from rel_path."""

_TOOL_RUN = """Read a PAknow run trace by run_id.

Use for audit/debugging: intents, routes, cited evidence IDs, failures, elapsed time, and
the stored answer. This is read-only."""

_TOOL_SOPS = """List PAknow procedural-memory SOPs, optionally filtered by lifecycle status.

Use for auditing which SOPs exist and their validation state. Do not claim a SOP is active
unless its status says so; auto-promotion is intentionally capped."""

_TOOL_HEALTH = """Check whether the PAknow HTTP service is reachable, and report its base URL
and latency. Use before a long session or when another PAknow tool fails."""


def build_server(settings: Settings | None = None) -> MCPServer:
    cfg = settings or Settings.from_env()
    client = PaknowClient(cfg)

    server = MCPServer(
        name="paknow",
        title="PAknow price-action evidence base",
        description=(
            "Evidence-first price-action knowledge base. Ask grounded questions with "
            "citations, inspect evidence, and retrieve original page/figure images."
        ),
        instructions=_INSTRUCTIONS,
        version=__version__,
    )

    # ------------------------------------------------------------------ #
    # answer tools
    # ------------------------------------------------------------------ #
    @server.tool(
        name="paknow_ask",
        title="Ask PAknow",
        description=_TOOL_ASK,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def paknow_ask(
        question: Question,
        ctx: Context,
        include_figures: bool = False,
    ) -> AskOutput:
        await _progress(ctx, 5, "Calling PAknow /query")
        try:
            data = await client.ask(question)
        except PaknowError as exc:
            raise ToolError(str(exc)) from exc
        result = _ask_output(data, cfg)
        images = await _with_figures(
            result, client, cfg, include_figures=include_figures
        )
        await _progress(ctx, 95, "PAknow answer received")
        return CallToolResult(
            content=[TextContent(type="text", text=render_ask(result, cfg.max_text_chars)), *images],
            structuredContent=result.model_dump(mode="json"),
        )

    @server.tool(
        name="paknow_chart",
        title="Analyze a chart with PAknow",
        description=_TOOL_CHART,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def paknow_chart(
        question: Question,
        ctx: Context,
        image_path: str | None = None,
        image_base64: str | None = None,
        mime_type: str = "image/png",
        filename: str = "chart.png",
        cutoff: Cutoff | None = None,
        include_figures: bool = False,
    ) -> AskOutput:
        image = _load_image(
            image_path, image_base64, mime_type, filename, cfg.max_image_bytes
        )
        await _progress(ctx, 5, "Calling PAknow /charts")
        try:
            data = await client.chart(question, image, cutoff)
        except PaknowError as exc:
            raise ToolError(str(exc)) from exc
        result = _ask_output(data, cfg)
        images = await _with_figures(
            result, client, cfg, include_figures=include_figures
        )
        await _progress(ctx, 95, "PAknow chart analysis received")
        return CallToolResult(
            content=[TextContent(type="text", text=render_ask(result, cfg.max_text_chars)), *images],
            structuredContent=result.model_dump(mode="json"),
        )

    # ------------------------------------------------------------------ #
    # retrieval / evidence tools
    # ------------------------------------------------------------------ #
    @server.tool(
        name="paknow_search",
        title="Search PAknow evidence",
        description=_TOOL_SEARCH,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def paknow_search(
        query: Annotated[str, "Standalone retrieval query; bilingual terms are welcome."],
        top_k: TopK = 8,
        doc_id: str | None = None,
        block_type: str | None = None,
    ) -> SearchOutput:
        try:
            data = await client.search(query, top_k, doc_id, block_type)
        except PaknowError as exc:
            raise ToolError(str(exc)) from exc
        hits = [_search_hit(hit) for hit in (data.get("hits") or [])]
        return CallToolResult(
            content=[TextContent(type="text", text=render_search(SearchOutput(query=query, count=len(hits), hits=hits), cfg.max_text_chars))],
            structuredContent=SearchOutput(
                query=query, count=len(hits), hits=hits
            ).model_dump(mode="json"),
        )

    @server.tool(
        name="paknow_evidence",
        title="Read PAknow evidence",
        description=_TOOL_EVIDENCE,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def paknow_evidence(evidence_id: str) -> EvidenceOutput:
        try:
            data = await client.evidence(evidence_id)
        except PaknowError as exc:
            raise ToolError(str(exc)) from exc
        result = _evidence_output(data, cfg)
        return CallToolResult(
            content=[TextContent(type="text", text=render_evidence(result, cfg.max_text_chars))],
            structuredContent=result.model_dump(mode="json"),
        )

    # ------------------------------------------------------------------ #
    # corpus / image / audit tools
    # ------------------------------------------------------------------ #
    @server.tool(
        name="paknow_documents",
        title="List PAknow documents",
        description=_TOOL_DOCUMENTS,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def paknow_documents() -> DocumentsOutput:
        try:
            rows = await client.documents()
        except PaknowError as exc:
            raise ToolError(str(exc)) from exc
        result = DocumentsOutput(
            count=len(rows),
            documents=[DocumentInfo(**row) for row in rows],
        )
        return CallToolResult(
            content=[TextContent(type="text", text=render_documents(result, cfg.max_text_chars))],
            structuredContent=result.model_dump(mode="json"),
        )

    @server.tool(
        name="paknow_source",
        title="Read PAknow document metadata",
        description=_TOOL_SOURCE,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def paknow_source(doc_id: str) -> SourceOutput:
        try:
            data = await client.source(doc_id)
        except PaknowError as exc:
            raise ToolError(str(exc)) from exc
        result = SourceOutput(**data)
        return CallToolResult(
            content=[TextContent(type="text", text=render_source(result, cfg.max_text_chars))],
            structuredContent=result.model_dump(mode="json"),
        )

    @server.tool(
        name="paknow_page_image",
        title="Render a PAknow PDF page",
        description=_TOOL_PAGE_IMAGE,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def paknow_page_image(
        doc_id: str,
        page: Annotated[int, "1-based PDF page number."],
        scale: Scale = 1.5,
    ) -> ImageOutput:
        try:
            data, mime = await client.page_image(doc_id, page, scale)
        except PaknowError as exc:
            raise ToolError(str(exc)) from exc
        result = ImageOutput(
            kind="page",
            doc_id=doc_id,
            page=page,
            scale=scale,
            mime_type=mime,
            byte_size=len(data),
        )
        return CallToolResult(
            content=[
                TextContent(type="text", text=render_image(result)),
                Image(data=data, format=_image_format(mime)).to_image_content(),
            ],
            structuredContent=result.model_dump(mode="json"),
        )

    @server.tool(
        name="paknow_figure_image",
        title="Read a PAknow figure image",
        description=_TOOL_FIGURE_IMAGE,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def paknow_figure_image(figure_id: str) -> ImageOutput:
        try:
            data, mime = await client.figure_image(figure_id)
        except PaknowError as exc:
            raise ToolError(str(exc)) from exc
        result = ImageOutput(
            kind="figure",
            figure_id=figure_id,
            mime_type=mime,
            byte_size=len(data),
        )
        return CallToolResult(
            content=[
                TextContent(type="text", text=render_image(result)),
                Image(data=data, format=_image_format(mime)).to_image_content(),
            ],
            structuredContent=result.model_dump(mode="json"),
        )

    @server.tool(
        name="paknow_run",
        title="Read a PAknow run trace",
        description=_TOOL_RUN,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def paknow_run(run_id: str) -> RunOutput:
        try:
            data = await client.run(run_id)
        except PaknowError as exc:
            raise ToolError(str(exc)) from exc
        result = RunOutput(
            run_id=str(data.get("run_id") or run_id),
            intents=list(data.get("intents") or []),
            routes=list(data.get("routes") or []),
            evidence_ids=list(data.get("evidence_ids") or []),
            output=data.get("output"),
            failures=list(data.get("failures") or []),
            elapsed_ms=data.get("elapsed_ms"),
        )
        return CallToolResult(
            content=[TextContent(type="text", text=render_run(result, cfg.max_text_chars))],
            structuredContent=result.model_dump(mode="json"),
        )

    @server.tool(
        name="paknow_sops",
        title="List PAknow SOPs",
        description=_TOOL_SOPS,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def paknow_sops(status: SOPStatus | None = None) -> SOPsOutput:
        try:
            rows = await client.sops(status)
        except PaknowError as exc:
            raise ToolError(str(exc)) from exc
        result = SOPsOutput(count=len(rows), sops=[SOPInfo(**row) for row in rows])
        return CallToolResult(
            content=[TextContent(type="text", text=render_sops(result, cfg.max_text_chars))],
            structuredContent=result.model_dump(mode="json"),
        )

    @server.tool(
        name="paknow_health",
        title="Check PAknow health",
        description=_TOOL_HEALTH,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def paknow_health() -> HealthOutput:
        try:
            data = await client.health()
        except PaknowError as exc:
            raise ToolError(str(exc)) from exc
        result = HealthOutput(
            status=str(data.get("status") or "unknown"),
            base_url=cfg.base_url,
            latency_ms=int(data.get("_latency_ms") or 0),
            server_version=data.get("version"),
        )
        return CallToolResult(
            content=[TextContent(type="text", text=render_health(result))],
            structuredContent=result.model_dump(mode="json"),
        )

    return server


# ---------------------------------------------------------------------- #
# helpers
# ---------------------------------------------------------------------- #
def _ask_output(data: dict[str, Any], cfg: Settings) -> AskOutput:
    raw = str(data.get("answer") or "")
    citations: list[Citation] = []
    for index, item in enumerate(data.get("citations") or []):
        citations.append(
            Citation(
                index=index,
                display_index=index + 1,
                evidence_id=str(item.get("evidence_id") or ""),
                citation=str(item.get("citation") or ""),
                text=str(item.get("text") or ""),
                doc_id=item.get("doc_id"),
            )
        )
    figures = [
        FigureRef(
            figure_id=str(item.get("figure_id") or ""),
            figure_label=item.get("figure_label"),
            rel_path=item.get("rel_path"),
            width_px=item.get("width_px"),
            height_px=item.get("height_px"),
            image_url=cfg.url(f"/figures/{item.get('figure_id')}/image"),
        )
        for item in (data.get("figures") or [])
    ]
    warnings: list[str] = []
    if not raw:
        warnings.append("PAknow returned an empty answer body.")
    if not str(data.get("run_id") or ""):
        warnings.append("PAknow did not return a run_id; audit tracing is unavailable.")
    return AskOutput(
        run_id=str(data.get("run_id") or ""),
        intent=data.get("intent"),
        is_framework=bool(data.get("is_framework")),
        routes=list(data.get("routes") or []),
        answer_raw=raw,
        answer_display=display_answer(raw, len(citations)),
        uncertainty=list(data.get("uncertainty") or []),
        citations=citations,
        figures=figures,
        citation_index_base=0,
        warnings=warnings,
    )


def _search_hit(item: dict[str, Any]) -> SearchHit:
    return SearchHit(
        evidence_id=str(item.get("evidence_id") or ""),
        doc_id=item.get("doc_id"),
        text=str(item.get("text") or ""),
        score=item.get("score"),
        block_type=item.get("block_type"),
        chapter_path=list(item.get("chapter_path") or []),
        pdf_page=item.get("pdf_page"),
        figure_label=item.get("figure_label"),
        epub_href=item.get("epub_href"),
        figure_ids=list(item.get("figure_ids") or []),
    )


def _evidence_output(data: dict[str, Any], cfg: Settings) -> EvidenceOutput:
    figures = [
        FigureRef(
            figure_id=str(item.get("figure_id") or ""),
            figure_label=item.get("label") or item.get("figure_label"),
            rel_path=item.get("rel_path"),
            image_url=cfg.url(f"/figures/{item.get('figure_id')}/image"),
        )
        for item in (data.get("figures") or [])
    ]
    return EvidenceOutput(
        evidence_id=str(data.get("evidence_id") or ""),
        text=str(data.get("text") or ""),
        block_type=data.get("block_type"),
        chapter_path=list(data.get("chapter_path") or []),
        pdf_page=data.get("pdf_page"),
        printed_page=data.get("printed_page"),
        figure_label=data.get("figure_label"),
        epub_href=data.get("epub_href"),
        doc_id=data.get("doc_id"),
        doc_title=data.get("doc_title"),
        figures=figures,
    )


async def _with_figures(
    result: AskOutput,
    client: PaknowClient,
    cfg: Settings,
    *,
    include_figures: bool,
) -> list[ImageContent]:
    images: list[ImageContent] = []
    if include_figures:
        for figure in result.figures[: cfg.max_figure_images]:
            try:
                data, mime = await client.figure_image(figure.figure_id)
            except PaknowError as exc:
                result.warnings.append(
                    f"Could not load figure {figure.figure_id}: {exc}"
                )
                continue
            images.append(
                Image(data=data, format=_image_format(mime)).to_image_content()
            )
    return images


def _load_image(
    image_path: str | None,
    image_base64: str | None,
    mime_type: str,
    filename: str,
    max_bytes: int,
) -> ChartImage:
    if image_path and image_base64:
        raise ToolError("Provide exactly one of image_path or image_base64.")
    if image_path:
        path = resolve_image_path(image_path)
        if path is None or not path.is_file():
            raise ToolError(f"Chart image not found: {image_path}")
        size = path.stat().st_size
        if size > max_bytes:
            raise ToolError(
                f"Chart image is {size} bytes; limit is {max_bytes} bytes."
            )
        guessed = mimetypes.guess_type(path.name)[0] or mime_type
        return ChartImage(filename=path.name, data=path.read_bytes(), mime_type=guessed)
    if image_base64:
        raw = image_base64.strip()
        if raw.startswith("data:"):
            header, _, payload = raw.partition(",")
            if not payload:
                raise ToolError("Invalid data URI for image_base64.")
            mime_type = header[5:].split(";")[0] or mime_type
            raw = payload
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ToolError(f"image_base64 is not valid base64: {exc}") from exc
        if not data:
            raise ToolError("image_base64 decoded to an empty payload.")
        if len(data) > max_bytes:
            raise ToolError(
                f"Chart image is {len(data)} bytes; limit is {max_bytes} bytes."
            )
        return ChartImage(filename=filename, data=data, mime_type=mime_type)
    raise ToolError("Chart analysis requires image_path or image_base64.")


def _image_format(mime_type: str) -> str:
    value = (mime_type or "image/png").split(";")[0].strip().lower()
    return value.split("/")[-1] or "png"


async def _progress(ctx: Context, progress: float, message: str) -> None:
    """Progress is best-effort; older clients may not support it."""
    try:
        await ctx.report_progress(progress, 100, message)
    except Exception:
        pass
