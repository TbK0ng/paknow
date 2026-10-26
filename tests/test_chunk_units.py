"""Chunked units keep their parent, so ``parent_id`` resolves at retrieval."""
from __future__ import annotations

from paknow.evidence.contracts import DocFormat, EvidenceUnit, SourceLocator
from paknow.ingest.chunk import chunk_units


def _long_unit(eid: str = "p1") -> EvidenceUnit:
    return EvidenceUnit(
        evidence_id=eid,
        doc_id="d1",
        text="第一句话。" * 300,  # 1500 chars, over the 1200 cap
        text_hash="h",
        locator=SourceLocator(doc_id="d1", format=DocFormat.pdf, pdf_page_1based=1),
    )


def test_parent_is_kept_and_links_its_children():
    units = chunk_units([_long_unit()])
    parents = [u for u in units if u.child_ids]
    assert len(parents) == 1
    parent = parents[0]
    children = [u for u in units if u.parent_id]
    assert [c.evidence_id for c in children] == parent.child_ids
    assert all(c.parent_id == parent.evidence_id for c in children)
    # children carry the text that gets embedded; the parent is context only
    assert len(parent.text) > 1200
    assert all(len(c.text) <= 1200 for c in children)


def test_short_unit_is_untouched():
    unit = EvidenceUnit(
        evidence_id="p2",
        doc_id="d1",
        text="短段落。",
        text_hash="h",
        locator=SourceLocator(doc_id="d1", format=DocFormat.pdf, pdf_page_1based=1),
    )
    assert chunk_units([unit]) == [unit]
