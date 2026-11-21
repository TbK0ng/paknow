"""Persist text passages and their reading-order neighbors."""
from __future__ import annotations

from paknow.db import SessionLocal
from paknow.db.models import EvidenceUnitRow
from paknow.evidence.contracts import EvidenceUnit


def persist_units(units: list[EvidenceUnit]) -> int:
    """Merge the parser output by evidence ID within a staging build."""
    seen: set[str] = set()
    ordered: list[EvidenceUnit] = []
    for u in units:
        if u.evidence_id in seen:
            continue
        seen.add(u.evidence_id)
        ordered.append(u)
    # reading-order adjacency (non-footer neighbors) for context expansion
    content = [u.evidence_id for u in ordered if u.block_type != "footer" and not u.locator.context_only]
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
        parsed_at=u.parsed_at,
        pdf_page_1based=u.locator.pdf_page_1based,
        printed_page_label=u.locator.printed_page_label,
        figure_label=u.locator.figure_label,
        epub_href=u.locator.epub_href,
        chapter_path=u.locator.chapter_path,
    )
