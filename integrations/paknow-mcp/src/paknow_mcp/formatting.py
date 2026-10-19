"""Human- and model-facing Markdown rendering.

Rules:
- Never invent a locator; only render what PAknow returned.
- Keep the backend's raw answer available in structuredContent.
- Convert 0-based [n] markers to 1-based display markers in the text channel.
- Cap quotes so a single tool result cannot flood an agent's context.
"""

from __future__ import annotations

import json
import re

from .models import (
    AskOutput,
    DocumentsOutput,
    EvidenceOutput,
    HealthOutput,
    ImageOutput,
    RunOutput,
    SOPsOutput,
    SearchOutput,
    SourceOutput,
)

_INDEX_RE = re.compile(r"\[(\d+)\]")
_QUOTE_CHARS = 240


def clip(text: str | None, limit: int = _QUOTE_CHARS) -> str:
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "…"


def display_answer(raw: str, citation_count: int) -> str:
    """Convert 0-based [n] to 1-based [n+1] when the citation exists."""

    def replace(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if 0 <= index < citation_count:
            return f"[{index + 1}]"
        return match.group(0)

    return _INDEX_RE.sub(replace, raw or "")


def locator(chapter_path: list[str], figure_label: str | None, page: int | None, printed: str | None, href: str | None) -> str:
    parts: list[str] = []
    if chapter_path:
        parts.append(" → ".join(chapter_path))
    if figure_label:
        parts.append(str(figure_label))
    if page:
        seg = f"PDF 第 {page} 页"
        if printed:
            seg += f"（章内印刷页 {printed}）"
        parts.append(seg)
    if href:
        parts.append(href)
    return " → ".join(parts) or "（无定位信息）"


def render_ask(output: AskOutput, max_chars: int) -> str:
    lines = [
        "答案：",
        output.answer_display or "（PAknow 未返回答案文本）",
    ]
    if output.uncertainty:
        lines += ["", "资料未说明 / 不确定："]
        lines += [f"- {item}" for item in output.uncertainty]

    if output.citations:
        lines += ["", "出处：" ]
        for citation in output.citations:
            lines.append(
                f"[{citation.display_index}] {citation.citation} "
                f"(evidence_id={citation.evidence_id})"
            )
            if citation.text:
                lines.append(f"> {clip(citation.text)}")
        lines += [
            "",
            "引用编号说明：正文已按 1-based 展示；结构化结果中的 "
            "citation_index_base=0，可据此回到后端原始编号。",
        ]
    else:
        lines += ["", "出处：本次未返回可引用证据。"]

    if output.figures:
        lines += ["", "相关图："]
        for figure in output.figures:
            label = f"图 {figure.figure_label}" if figure.figure_label else figure.figure_id
            lines.append(f"- {label} (figure_id={figure.figure_id})")

    if output.warnings:
        lines += ["", "适配层警告："]
        lines += [f"- {warning}" for warning in output.warnings]

    return _cap("\n".join(lines), max_chars)


def render_search(output: SearchOutput, max_chars: int) -> str:
    if not output.hits:
        return f"检索“{output.query}”没有命中证据。"
    lines = [f"检索：{output.query}", f"命中：{output.count} 条", ""]
    for i, hit in enumerate(output.hits, start=1):
        title = locator(
            hit.chapter_path, hit.figure_label, hit.pdf_page, None, hit.epub_href
        )
        lines.append(f"[{i}] {title} (evidence_id={hit.evidence_id})")
        if hit.score is not None:
            lines.append(f"    score={hit.score:.4f}")
        lines.append(f"    {clip(hit.text)}")
        lines.append("")
    return _cap("\n".join(lines), max_chars)


def render_evidence(output: EvidenceOutput, max_chars: int) -> str:
    lines = [
        f"证据：{output.evidence_id}",
        f"来源：{output.doc_title or output.doc_id or '未知'}",
        f"定位：{locator(output.chapter_path, output.figure_label, output.pdf_page, output.printed_page, output.epub_href)}",
        f"类型：{output.block_type or '未知'}",
        "",
        output.text,
    ]
    if output.figures:
        lines += ["", "关联图："]
        for figure in output.figures:
            label = f"图 {figure.figure_label}" if figure.figure_label else figure.figure_id
            lines.append(f"- {label} (figure_id={figure.figure_id})")
    return _cap("\n".join(lines), max_chars)


def render_documents(output: DocumentsOutput, max_chars: int) -> str:
    if not output.documents:
        return "资料库中没有文档。"
    lines = [f"资料库文档：{output.count} 本", ""]
    for doc in output.documents:
        meta = " · ".join(
            part
            for part in [
                doc.author,
                doc.language,
                doc.format,
                f"{doc.page_count} 页" if doc.page_count else None,
                f"{doc.entry_count} 证据单元" if doc.entry_count else None,
            ]
            if part
        )
        lines.append(f"- {doc.title or doc.doc_id} ({doc.doc_id})")
        if meta:
            lines.append(f"  {meta}")
    return _cap("\n".join(lines), max_chars)


def render_source(output: SourceOutput, max_chars: int) -> str:
    lines = [
        f"{output.title or output.doc_id}",
        f"doc_id: {output.doc_id}",
        f"作者: {output.author or '未记录'}",
        f"译者: {output.translator or '未记录'}",
        f"语言: {output.language or '未记录'}",
        f"格式: {output.format or '未记录'}",
        f"版本: {output.edition or '未记录'}",
        f"页数: {output.page_count if output.page_count is not None else '未记录'}",
        f"证据单元: {output.evidence_units if output.evidence_units is not None else '未记录'}",
        f"图资产: {output.figures if output.figures is not None else '未记录'}",
    ]
    return _cap("\n".join(lines), max_chars)


def render_health(output: HealthOutput) -> str:
    return (
        f"PAknow: {output.status}\n"
        f"地址: {output.base_url}\n"
        f"延迟: {output.latency_ms} ms\n"
        f"版本: {output.server_version or '未提供'}"
    )


def render_run(output: RunOutput, max_chars: int) -> str:
    payload = output.model_dump(mode="json")
    return _cap(json.dumps(payload, ensure_ascii=False, indent=2), max_chars)


def render_sops(output: SOPsOutput, max_chars: int) -> str:
    if not output.sops:
        return "没有匹配的 SOP。"
    lines = [f"SOP：{output.count} 条", ""]
    for sop in output.sops:
        lines.append(
            f"- {sop.sop_id} v{sop.version} [{sop.status}] scope={sop.intent_scope}"
        )
    return _cap("\n".join(lines), max_chars)


def render_image(output: ImageOutput) -> str:
    if output.kind == "page":
        return (
            f"PDF 页图：doc_id={output.doc_id} page={output.page} "
            f"scale={output.scale} ({output.mime_type}, {output.byte_size} bytes)"
        )
    return (
        f"图资产：figure_id={output.figure_id} label={output.figure_label or '?'} "
        f"({output.mime_type}, {output.byte_size} bytes)"
    )


def _cap(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return (
        text[:max_chars].rstrip()
        + f"\n\n[输出已截断：{len(text)} → {max_chars} 字符；完整数据见 structuredContent]"
    )
