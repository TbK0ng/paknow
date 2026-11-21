"""Citation rendering: evidence id -> human-readable source location (plan §7.3)."""
from __future__ import annotations

from typing import Any

from paknow.db import SessionLocal
from paknow.db.models import DocumentRow, EvidenceUnitRow
from paknow.evidence.contracts import Provenance


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
        href = unit["epub_href"]
        fragment = unit.get("epub_fragment")
        parts.append(f"{href}#{fragment}" if fragment else href)
    return " → ".join(parts)


def citation_ready_hits(candidates: list[dict[str, Any]], *, doc_ids: frozenset[str] | None = None,
                        allow_parent: bool = False) -> list[dict[str, Any]]:
    """Resolve citable original text from the authoritative metadata DB."""
    ids = {hit["evidence_id"] for hit in candidates if hit.get("evidence_id")}
    if not ids:
        return []
    with SessionLocal() as session:
        query = (
            session.query(EvidenceUnitRow, DocumentRow)
            .join(DocumentRow, EvidenceUnitRow.doc_id == DocumentRow.doc_id)
            .filter(EvidenceUnitRow.evidence_id.in_(ids))
            .filter(EvidenceUnitRow.provenance.in_([Provenance.native_text.value, Provenance.ocr.value]))
        )
        if doc_ids is not None:
            query = query.filter(EvidenceUnitRow.doc_id.in_(doc_ids))
        rows = query.all()
        metadata = {
            unit.evidence_id: {
                "doc_id": doc.doc_id,
                "doc_title": doc.title,
                "provenance": unit.provenance,
                "text": unit.text,
                "block_type": unit.block_type,
                "text_hash": unit.text_hash,
                "passage_id": unit.parent_id or unit.evidence_id,
                "parent_id": unit.parent_id,
                "locator": unit.locator or {},
                "chapter_path": unit.chapter_path or [],
                "pdf_page": unit.pdf_page_1based,
                "printed_page": unit.printed_page_label or (unit.locator or {}).get("printed_page_label"),
                "epub_href": unit.epub_href,
                "epub_fragment": (unit.locator or {}).get("epub_fragment"),
                "figure_label": unit.figure_label,
            }
            for unit, doc in rows
            if unit.text.strip() and unit.block_type != "footer" and (allow_parent or not unit.child_ids)
        }
    return [{**hit, **metadata[hit["evidence_id"]]} for hit in candidates if hit.get("evidence_id") in metadata]
