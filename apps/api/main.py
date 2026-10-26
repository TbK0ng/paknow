"""FastAPI service (plan §12.2)."""
from __future__ import annotations

import io
import mimetypes
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from PIL import Image
from pydantic import BaseModel, Field
from qdrant_client.models import FieldCondition, Filter, MatchValue

from paknow.db import SessionLocal
from paknow.db.models import DocumentRow, EvidenceUnitRow, FigureAssetRow, RunTraceRow
from paknow.evidence.citations import resolve_citations
from paknow.gateway import gateway
from paknow.ingest.store import object_path
from paknow.memory.sop import list_sops
from paknow.retrieval.qdrant_store import StoreLockedError, check_store_ready
from paknow.retrieval.search import diversify, hybrid_search, point_to_doc, rerank
from paknow.retrieval.visual import mask_right_bytes
from paknow.workflows.graph import run_query


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Refuse to serve a store we cannot answer from.

    A process that is up but replies empty is indistinguishable from a working
    one, so a missing, empty or locked store stops startup with its own reason
    instead of degrading into that state.
    """
    check_store_ready()
    yield


app = FastAPI(title="PAknow", version="0.1.0", lifespan=_lifespan)


@app.exception_handler(StoreLockedError)
async def _store_locked(_request: Request, exc: StoreLockedError) -> JSONResponse:
    """The embedded store belongs to another process: retry later, never an empty 200.

    In the shipped FastAPI app startup refusal normally happens first (the
    lifespan checks the store); this handler is the single mapping point and
    still covers a lock acquired after startup.
    """
    return JSONResponse(status_code=503, content={"detail": str(exc)})


_VIEWER_INDEX = Path(__file__).resolve().parent.parent / "viewer" / "index.html"

#: Turns of the current dialogue sent to the standalone-query rewriter.
MAX_HISTORY_TURNS = 8


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class QueryRequest(BaseModel):
    query: str
    history: list[ChatTurn] = Field(default_factory=list)
    # opt-in: the chain internals are diagnostic, not part of the normal reply
    debug: bool = False


class StandaloneQuery(BaseModel):
    query: str


class SearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=8, ge=1, le=20)
    doc_id: str | None = None
    block_type: str | None = None


def _standalone_query(query: str, history: list[ChatTurn]) -> tuple[str, bool]:
    """Rewrite a follow-up into a standalone question using recent turns.

    Retrieval only sees the rewritten question, so pronouns and elisions in the
    follow-up ("那它呢") do not silently degrade recall. Any failure falls back
    to the raw question: the dialogue still works, it just loses the context.
    """
    if not history:
        return query, False
    turns = [turn for turn in history[-MAX_HISTORY_TURNS:] if turn.content.strip()]
    if not turns:
        return query, False
    transcript = "\n".join(
        f"{'用户' if turn.role == 'user' else '助手'}：{turn.content.strip()}"
        for turn in turns
    )
    try:
        out = gateway.chat_json(
            StandaloneQuery,
            [
                {
                    "role": "system",
                    "content": (
                        "你是检索查询改写器。根据对话历史，把用户的最新问题改写成"
                        "一个可独立检索的中文问题。只补全指代与省略，不回答、不解释、"
                        "不添加历史里没有的事实。"
                    ),
                },
                {
                    "role": "user",
                    "content": f"对话历史：\n{transcript}\n\n最新问题：{query}",
                },
            ],
            max_tokens=256,
        )
        rewritten = (out.query or "").strip()
    except Exception:  # noqa: BLE001 - a failed rewrite must not break the answer
        return query, False
    if not rewritten or rewritten == query:
        return query, False
    return rewritten, True


def _graph_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Compact graph-expansion row for the viewer; evidence text is truncated."""
    return {
        "evidence_id": entry.get("evidence_id"),
        "relation": entry.get("relation"),
        "from_label": entry.get("from_label"),
        "to_label": entry.get("to_label"),
        "claim": entry.get("claim"),
        "concept": entry.get("concept"),
        "source": entry.get("source"),
        "seen_index": entry.get("seen_index"),
        "doc_id": entry.get("doc_id"),
        "chapter_path": entry.get("chapter_path") or [],
        "pdf_page": entry.get("pdf_page"),
        "printed_page": entry.get("printed_page"),
        "figure_label": entry.get("figure_label"),
        "text": (entry.get("text") or "")[:240],
    }


_FAILURE_REASON = re.compile(r"^(timeout|invalid_structure|no_content)(?:: | \(|$)")


def _failure_event(entry: str) -> dict[str, str]:
    """``"stage: reason: detail"`` -> the structured row the debug view returns."""
    stage, _, rest = entry.partition(": ")
    match = _FAILURE_REASON.match(rest)
    if match is None:
        # retrieval/expansion failures carry no model-output classification
        return {"stage": stage, "category": "error", "detail": rest}
    return {
        "stage": stage,
        "category": match.group(1),
        "detail": rest[match.end():].strip(" )"),
    }


def _debug_block(result: dict[str, Any]) -> dict[str, Any]:
    """Read-only chain internals: what was recalled, judged, and failed."""
    return {
        "hits": [
            {
                "evidence_id": hit.get("evidence_id"),
                "score": hit.get("score"),
                "block_type": hit.get("block_type"),
            }
            for hit in result.get("hits") or []
        ],
        "citable_ids": list(result.get("citable_ids") or []),
        "context_unit_counts": result.get("context_unit_counts") or {},
        "sufficiency": result.get("sufficiency") or {},
        "failures": [_failure_event(entry) for entry in result.get("failures") or []],
        "rounds": result.get("rounds"),
        "graph_stats": result.get("graph_stats") or {},
    }


def _resolve_answer(result: dict[str, Any]) -> dict[str, Any]:
    ans = result.get("answer") or {}
    citations = resolve_citations(ans.get("evidence_ids") or [])
    return {
        "run_id": result.get("run_id"),
        "intent": result.get("intent"),
        "is_framework": result.get("is_framework"),
        "routes": result.get("routes"),
        "answer": ans.get("answer"),
        "uncertainty": ans.get("uncertainty"),
        "citations": citations,
        "figures": result.get("figures"),
        "rounds": result.get("rounds"),
        "graph_stats": result.get("graph_stats") or {},
        "graph": [_graph_entry(e) for e in (result.get("graph_evidence") or [])],
        # a placeholder answer is marked, never inferred from its length
        "degraded": bool(ans.get("degraded")),
        "degradation": ans.get("degradation"),
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/query")
def query(req: QueryRequest) -> dict[str, Any]:
    standalone, rewritten = _standalone_query(req.query, req.history)
    result = run_query(standalone)
    payload = _resolve_answer(result)
    payload["query_used"] = standalone
    payload["rewritten"] = rewritten
    if req.debug:
        payload["debug"] = _debug_block(result)
    return payload


@app.post("/search")
def search(req: SearchRequest) -> dict[str, Any]:
    """Retrieval-only search. No routing or generation, so agents can inspect
    evidence without paying for a full answer pipeline."""
    must = []
    if req.doc_id:
        must.append(FieldCondition(key="doc_id", match=MatchValue(value=req.doc_id)))
    if req.block_type:
        must.append(FieldCondition(key="block_type", match=MatchValue(value=req.block_type)))
    query_filter = Filter(must=must) if must else None

    candidates = [
        point_to_doc(p)
        for p in hybrid_search(req.query, top_k=40, query_filter=query_filter)
    ]
    if len(candidates) >= 2:
        try:
            candidates = rerank(req.query, candidates[:20], top_k=20)
        except Exception:
            # Retrieval must still work when the local reranker is unavailable.
            pass
    hits = diversify(candidates, top_k=req.top_k, max_per_doc=5)
    return {"query": req.query, "count": len(hits), "hits": hits}


@app.get("/documents")
def documents() -> list[dict[str, Any]]:
    with SessionLocal() as s:
        rows = s.query(DocumentRow).order_by(DocumentRow.title).all()
    return [
        {
            "doc_id": d.doc_id,
            "title": d.title,
            "author": d.author,
            "translator": d.translator,
            "language": d.language,
            "format": d.format,
            "edition": d.edition,
            "page_count": d.page_count,
            "entry_count": d.entry_count,
        }
        for d in rows
    ]


@app.post("/charts")
async def charts(query: str, file: UploadFile, cutoff: float = 0.0) -> dict[str, Any]:
    """Chart upload analysis. ``cutoff`` (0..1) masks the rightmost fraction
    of the image so bars after the analysis point are invisible (plan §8.4)."""
    data = await file.read()
    try:
        Image.open(io.BytesIO(data)).verify()
    except Exception:
        raise HTTPException(400, "upload is not a readable image") from None
    if cutoff > 0:
        data = mask_right_bytes(data, cutoff)
    result = run_query(query, images=[data])
    payload = _resolve_answer(result)
    # the structured read is audit data for the chart surface only; /query keeps
    # its text-answer shape
    payload["chart_read"] = result.get("chart_read")
    payload["chart_read_status"] = result.get("chart_read_status", "unstructured")
    return payload


@app.get("/evidence/{evidence_id}")
def evidence(evidence_id: str) -> dict[str, Any]:
    with SessionLocal() as s:
        u = s.get(EvidenceUnitRow, evidence_id)
        if u is None:
            raise HTTPException(404, "evidence not found")
        figs = (
            s.query(FigureAssetRow).filter(FigureAssetRow.figure_id.in_(u.figure_ids or [])).all()
            if u.figure_ids
            else []
        )
        doc = s.get(DocumentRow, u.doc_id)
    return {
        "evidence_id": u.evidence_id,
        "text": u.text,
        "block_type": u.block_type,
        "chapter_path": u.chapter_path or [],
        "pdf_page": u.pdf_page_1based,
        "printed_page": (u.locator or {}).get("printed_page_label"),
        "figure_label": u.figure_label,
        "epub_href": u.epub_href,
        "doc_id": u.doc_id,
        "doc_title": doc.title if doc else None,
        "figures": [
            {"figure_id": f.figure_id, "rel_path": f.rel_path, "label": f.figure_label}
            for f in figs
        ],
    }


@app.get("/sources/{doc_id}")
def source(doc_id: str) -> dict[str, Any]:
    with SessionLocal() as s:
        d = s.get(DocumentRow, doc_id)
        if d is None:
            raise HTTPException(404, "source not found")
        n_units = s.query(EvidenceUnitRow).filter(EvidenceUnitRow.doc_id == doc_id).count()
        n_figs = s.query(FigureAssetRow).filter(FigureAssetRow.doc_id == doc_id).count()
    return {
        "doc_id": d.doc_id,
        "title": d.title,
        "author": d.author,
        "translator": d.translator,
        "language": d.language,
        "format": d.format,
        "page_count": d.page_count,
        "entry_count": d.entry_count,
        "evidence_units": n_units,
        "figures": n_figs,
    }


@app.get("/runs/{run_id}")
def run(run_id: str) -> dict[str, Any]:
    with SessionLocal() as s:
        r = s.get(RunTraceRow, run_id)
        if r is None:
            raise HTTPException(404, "run not found")
        return {
            "run_id": r.run_id,
            "intents": r.intents,
            "routes": r.routes,
            "evidence_ids": r.evidence_ids,
            "output": r.output,
            "failures": r.failures,
            "elapsed_ms": r.elapsed_ms,
        }


@app.get("/sops")
def sops() -> list[dict[str, Any]]:
    return list_sops()


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(_VIEWER_INDEX)


@app.get("/figures/{figure_id}/image")
def figure_image(figure_id: str) -> FileResponse:
    """Serve a stored figure asset by id. Used by remote agent clients that
    cannot resolve ``FigureAssetRow.rel_path`` on their own filesystem."""
    with SessionLocal() as s:
        row = s.get(FigureAssetRow, figure_id)
        if row is None:
            raise HTTPException(404, "figure not found")
        rel_path = row.rel_path
    path = object_path(rel_path)
    if not path.is_file():
        raise HTTPException(404, "figure object missing")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type)


@app.get("/sources/{doc_id}/page/{page}/image")
def page_image(doc_id: str, page: int, scale: float = 1.5) -> Response:
    """Render a PDF page to PNG for the local viewer."""
    import fitz  # PyMuPDF

    with SessionLocal() as s:
        doc = s.get(DocumentRow, doc_id)
        if doc is None or doc.format != "pdf":
            raise HTTPException(404, "PDF source not found")
        src = doc.source_path
    d = fitz.open(src)
    try:
        if page < 1 or page > d.page_count:
            raise HTTPException(404, "page out of range")
        pix = d[page - 1].get_pixmap(matrix=fitz.Matrix(scale, scale))
        return Response(content=pix.tobytes("png"), media_type="image/png")
    finally:
        d.close()
