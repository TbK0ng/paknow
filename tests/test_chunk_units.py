"""Chunked units keep their parent, so ``parent_id`` resolves at retrieval."""
from __future__ import annotations

from paknow.evidence.contracts import DocFormat, EvidenceUnit, SourceLocator
from paknow.ingest.chunk import chunk_text, chunk_units


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


def test_adjacent_qualification_merges_verbatim_with_source_blocks_and_hard_page_boundary():
    def line(eid, text, page, box):
        return EvidenceUnit(evidence_id=eid, doc_id="doc", text=text, text_hash=eid,
                            locator=SourceLocator(doc_id="doc", format=DocFormat.pdf,
                                                  pdf_page_1based=page, bbox=box))
    a = line("a", "趋势回调有机会延续。", 1, (10, 10, 120, 20))
    b = line("b", "但在区间上沿，突破没有跟进可能失败。", 1, (12, 23, 140, 33))
    c = line("c", "下一页单独处理。", 2, (10, 37, 120, 47))
    raw_a, raw_b, passage, separate = chunk_units([a, b, c])
    assert separate == c
    assert all(raw.locator.context_only and raw.parent_id == passage.evidence_id for raw in (raw_a, raw_b))
    assert passage.locator.source_ids == ["a", "b"]
    assert passage.text == a.text + "\n" + b.text


def test_english_limit_clause_stays_with_antecedent_and_offsets_match_parent():
    text = "The trend continued after the pullback. However, weak follow-through is not confirmation. "
    parts = chunk_text(text * 30, max_chars=200)
    assert "".join(parts) == text * 30
    assert not any(part.startswith("However") for part in parts)
    parent, *children = chunk_units([_long_unit()])
    for child in children:
        a, b = child.locator.span_start, child.locator.span_end
        assert child.text == parent.text[a:b]


def test_figure_filter_drops_labels_not_unspaced_chinese_captions():
    reference = _long_unit("bare").model_copy(update={"text": "Figure A.2"})
    caption = _long_unit("caption").model_copy(update={"text": "图1显示突破失败。", "block_type": "caption"})
    assert chunk_units([reference, caption]) == [caption]


def test_adjacent_exception_stays_in_shared_parent_across_normal_length_cap():
    locator = SourceLocator(doc_id="d1", format=DocFormat.epub, epub_href="same.xhtml")
    left = _long_unit("left").model_copy(update={
        "text": "开头不是结论。" + "条件。" * 390 + "只能在趋势中成立。", "locator": locator})
    right = _long_unit("right").model_copy(update={
        "text": "但在区间里，缺乏跟进时不能照搬同一套结论，需要确认是否真的回到区间。", "locator": locator})
    result = chunk_units([left, right])
    a, b = result[:2]
    assert a.locator.context_only and b.locator.context_only and a.parent_id == b.parent_id
    parent = next(unit for unit in result if unit.evidence_id == a.parent_id)
    assert parent.text == left.text + "\n" + right.text and len(parent.text) > 1200
    assert any(right.text in u.text for u in result[2:] if not u.child_ids)
