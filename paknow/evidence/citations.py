"""Citation rendering: evidence id -> human-readable source location (plan §7.3)."""
from __future__ import annotations

from typing import Any

from paknow.db import SessionLocal
from paknow.db.models import DocumentRow, EvidenceUnitRow


def render_citation(unit: dict[str, Any], doc_title: str | None = None) -> str:
    parts: list[str] = []
    if doc_title:
        parts.append(f"《{doc_title}》")
    if unit.get("chapter_path"):
        parts.append(" → ".join(unit["chapter_path"]))
    if unit.get("figure_label"):
        parts.append(str(unit["figure_label"]))
    if unit.get("pdf_page"):
        seg = f"PDF 第 {unit['pdf_page']} 页"
        if unit.get("printed_page"):
            seg += f"（章内印刷页 {unit['printed_page']}）"
        parts.append(seg)
    if unit.get("epub_href"):
        parts.append(unit["epub_href"])
    return " → ".join(parts)


def resolve_citations(evidence_ids: list[str]) -> list[dict[str, Any]]:
    """Map evidence ids to rendered citations, preserving citation order."""
    if not evidence_ids:
        return []
    with SessionLocal() as s:
        units = {
            u.evidence_id: u
            for u in s.query(EvidenceUnitRow).filter(EvidenceUnitRow.evidence_id.in_(evidence_ids)).all()
        }
        doc_ids = {u.doc_id for u in units.values()}
        docs = {
            d.doc_id: d.title
            for d in s.query(DocumentRow).filter(DocumentRow.doc_id.in_(doc_ids)).all()
        }
    out: list[dict[str, Any]] = []
    for eid in evidence_ids:
        u = units.get(eid)
        if u is None:
            continue
        ud = {
            "chapter_path": u.chapter_path or [],
            "figure_label": u.figure_label,
            "pdf_page": u.pdf_page_1based,
            "printed_page": (u.locator or {}).get("printed_page_label"),
            "epub_href": u.epub_href,
        }
        out.append(
            {
                "evidence_id": eid,
                "citation": render_citation(ud, docs.get(u.doc_id)),
                "text": u.text,
                "doc_id": u.doc_id,
            }
        )
    return out
