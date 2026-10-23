"""Persist parse results (Pydantic -> PostgreSQL). Idempotent by primary key."""
from __future__ import annotations

from paknow.db import SessionLocal
from paknow.db.models import EvidenceUnitRow, FigureAssetRow
from paknow.evidence.contracts import EvidenceUnit, FigureAsset


def persist_units(units: list[EvidenceUnit]) -> int:
    seen: set[str] = set()
    ordered: list[EvidenceUnit] = []
    for u in units:
        if u.evidence_id in seen:
            continue
        seen.add(u.evidence_id)
        ordered.append(u)
    # reading-order adjacency (non-footer neighbors) for context expansion
    content = [u.evidence_id for u in ordered if u.block_type != "footer"]
    pos = {eid: i for i, eid in enumerate(content)}
    for u in ordered:
        if u.block_type == "footer" or u.evidence_id not in pos:
            continue
        i = pos[u.evidence_id]
        u.adjacent_ids = [e for e in (content[i - 1] if i else None, content[i + 1] if i + 1 < len(content) else None) if e]
    with SessionLocal() as s:
        for u in ordered:
            s.merge(_unit_row(u))
        s.commit()
    return len(units)


def persist_figures(figures: list[FigureAsset]) -> int:
    seen: set[str] = set()
    with SessionLocal() as s:
        for f in figures:
            if f.figure_id in seen:
                continue
            seen.add(f.figure_id)
            s.merge(_figure_row(f))
        s.commit()
    return len(figures)


def _unit_row(u: EvidenceUnit) -> EvidenceUnitRow:
    return EvidenceUnitRow(
        evidence_id=u.evidence_id,
        doc_id=u.doc_id,
        locator=u.locator.model_dump(),
        text=u.text,
        text_hash=u.text_hash,
        block_type=u.block_type,
        parent_id=u.parent_id,
        child_ids=u.child_ids,
        token_count=u.token_count,
        provenance=u.provenance.value,
        adjacent_ids=u.adjacent_ids,
        figure_ids=u.figure_ids,
        parsed_at=u.parsed_at,
        pdf_page_1based=u.locator.pdf_page_1based,
        printed_page_label=u.locator.printed_page_label,
        figure_label=u.locator.figure_label,
        epub_href=u.locator.epub_href,
        chapter_path=u.locator.chapter_path,
    )


def _figure_row(f: FigureAsset) -> FigureAssetRow:
    return FigureAssetRow(
        figure_id=f.figure_id,
        doc_id=f.doc_id,
        locator=f.locator.model_dump(),
        image_hash=f.image_hash,
        rel_path=f.rel_path,
        width_px=f.width_px,
        height_px=f.height_px,
        page_bbox=list(f.page_bbox) if f.page_bbox else None,
        caption_ids=f.caption_ids,
        explanation_ids=f.explanation_ids,
        annotation_source=f.annotation_source,
        vlm_description=f.vlm_description,
        description_model=f.description_model,
        is_decorative=f.is_decorative,
        registered_at=f.registered_at,
        pdf_page_1based=f.locator.pdf_page_1based,
        figure_label=f.locator.figure_label,
    )
