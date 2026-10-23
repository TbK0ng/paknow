"""Concept taxonomy: heading dedupe + deterministic grammar parsing + queries.

Parser tests are pure (batch of titles in, batch of edges out). One DB-backed
test covers rebuild idempotency, seed preservation, and the query surface.
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from paknow.db import Base
from paknow.db.models import DocumentRow, EvidenceUnitRow, GraphEdgeRow, GraphNodeRow
from paknow.graph import taxonomy
from paknow.graph.contracts import Relation
from paknow.graph.identity import concept_node_id
from paknow.graph.taxonomy import (
    HeadingUnit,
    build_taxonomy_edges,
    dedupe_headings,
    is_toc_title,
    parse_heading,
)


def _unit(eid: str, text: str, doc: str = "d1") -> HeadingUnit:
    return HeadingUnit(evidence_id=eid, doc_id=doc, text=text)


def test_dedupe_keeps_first_and_drops_page_headers():
    units = [
        _unit("a", "18根K线区间下方的突破失败：随后向上突破"),
        _unit("b", "18根K线区间下方的突破失败：随后向上突破"),  # duplicate in doc
        _unit("c", "18根K线区间下方的突破失败：随后向上突破", doc="d2"),  # other doc
        *[_unit(f"h{i}", "@喻无为") for i in range(5)],  # page header, 5x
        _unit("z", "  18根K线区间下方的突破失败：随后向上突破  "),  # whitespace-normalized dup
    ]

    result = dedupe_headings(units)

    assert [u.evidence_id for u in result.kept] == ["a", "c"]
    assert result.stats["headings_in"] == 9
    assert result.stats["dropped_header"] == 5
    assert result.stats["dropped_duplicate"] == 2
    assert result.stats["headings_after_dedupe"] == 2


def test_failure_marker_maps_to_invalidated_by():
    parsed = parse_heading("空头通道下方突破失败：竭尽性卖出高潮")

    assert parsed is not None
    assert parsed.relation is Relation.INVALIDATED_BY
    assert parsed.from_concept == "failed_breakout"
    assert parsed.to_concept == "climax"


def test_later_marker_maps_to_transitions_to():
    parsed = parse_heading("18根K线区间下方的突破失败：随后向上突破")

    assert parsed is not None
    assert parsed.relation is Relation.TRANSITIONS_TO
    assert parsed.from_concept == "failed_breakout"
    assert parsed.to_concept == "breakout"


def test_exception_marker_maps_to_exception_to():
    parsed = parse_heading("多头通道：常常形成楔形顶部，但仍继续上涨")

    assert parsed is not None
    assert parsed.relation is Relation.EXCEPTION_TO
    assert parsed.from_concept == "channel"
    assert parsed.to_concept == "wedge"


def test_variant_marker_maps_to_part_of():
    parsed = parse_heading("双顶上方突破失败：楔形顶的一种变体")

    assert parsed is not None
    assert parsed.relation is Relation.PART_OF
    assert parsed.from_concept == "failed_breakout"
    assert parsed.to_concept == "wedge"


def test_unrecognised_heading_is_dropped_not_defaulted():
    # no colon; self-edge; and no resolvable parent concept
    assert parse_heading("无法识别的标题") is None
    assert parse_heading("ATH：抛物线楔形顶失败") is None
    assert parse_heading("双底回调：三角形的一种变体") is None


def test_build_edges_is_order_independent_and_reports_coverage():
    units = [
        _unit("a", "18根K线区间下方的突破失败：随后向上突破"),
        _unit("b", "向下跳空：卖出高潮，但向上反转失败"),
        _unit("c", "多头通道：常常形成楔形顶部，但仍继续上涨"),
        _unit("d", "无法识别的标题"),
    ]

    forward = build_taxonomy_edges(units)
    backward = build_taxonomy_edges(list(reversed(units)))

    assert {(e.from_concept, e.relation, e.to_concept) for e in forward.edges} == {
        (e.from_concept, e.relation, e.to_concept) for e in backward.edges
    }
    assert forward.stats["headings_after_dedupe"] == 4
    assert forward.stats["parsed"] == 3
    assert forward.stats["dropped_unparsed"] == 1
    assert forward.stats["edges"] == 3
    assert all(e.origin == "heading_parse" and e.source for e in forward.edges)


def test_rebuild_preserves_seed_edges_and_is_idempotent(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(taxonomy, "SessionLocal", Session)

    with Session() as s:
        s.add(DocumentRow(doc_id="d1", content_hash="h", format="pdf", source_path="x"))
        for eid, text in [
            ("e1", "18根K线区间下方的突破失败：随后向上突破"),
            ("e2", "向下跳空：卖出高潮，但向上反转失败"),
            ("e3", "多头通道：常常形成楔形顶部，但仍继续上涨"),
        ]:
            s.add(
                EvidenceUnitRow(
                    evidence_id=eid,
                    doc_id="d1",
                    locator={"doc_id": "d1", "format": "pdf"},
                    text=text,
                    text_hash=eid,
                    block_type="heading",
                )
            )
        seed_from = concept_node_id("trend")
        seed_to = concept_node_id("trading_range")
        s.add_all(
            [
                GraphNodeRow(node_id=seed_from, node_type="concept", label="趋势", concept_id="trend"),
                GraphNodeRow(node_id=seed_to, node_type="concept", label="交易区间", concept_id="trading_range"),
                GraphEdgeRow(
                    edge_id="seed-edge",
                    from_node=seed_from,
                    to_node=seed_to,
                    relation="TRANSITIONS_TO",
                    source="seed",
                    origin="seed",
                ),
            ]
        )
        s.commit()

    first = taxonomy.rebuild_taxonomy()
    second = taxonomy.rebuild_taxonomy()

    assert first["edges"] == second["edges"] == 3
    with Session() as s:
        edges = s.query(GraphEdgeRow).all()
        by_origin = {}
        for e in edges:
            by_origin.setdefault(e.origin, set()).add((e.from_node, e.to_node, e.relation, e.source))
        assert by_origin["seed"] == {(seed_from, seed_to, "TRANSITIONS_TO", "seed")}
        assert len(by_origin["heading_parse"]) == 3
        nodes = {n.concept_id for n in s.query(GraphNodeRow).filter(GraphNodeRow.node_type == "concept")}
        assert {"failed_breakout", "breakout", "gap", "reversal", "channel", "wedge"} <= nodes

    neighbors = taxonomy.taxonomy_neighbors(["failed_breakout"])
    rels = {(n["relation"], n["to"]["concept_id"], n["direction"]) for n in neighbors}
    assert ("TRANSITIONS_TO", "breakout", "out") in rels
    assert all(n["evidence_id"] for n in neighbors)


def test_shared_alias_resolves_to_primary_concept_not_auto_duplicate():
    # configs/terms.yaml has both trading_range and auto_trading_range_tr for
    # 「交易区间」; the parser must not mint a second node carrying the same label
    parsed = parse_heading("楔形底下方的突破失败：交易区间")

    assert parsed is not None
    assert parsed.relation is Relation.INVALIDATED_BY
    assert parsed.to_concept == "trading_range"


def _memory_session(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(taxonomy, "SessionLocal", Session)
    return Session


def _add_doc(Session, doc_id: str, headings: list[tuple[str, str]]) -> None:
    with Session() as s:
        s.add(DocumentRow(doc_id=doc_id, content_hash=doc_id, format="pdf", source_path=f"{doc_id}.pdf"))
        for eid, text in headings:
            s.add(
                EvidenceUnitRow(
                    evidence_id=eid,
                    doc_id=doc_id,
                    locator={"doc_id": doc_id, "format": "pdf", "pdf_page_1based": 1},
                    text=text,
                    text_hash=eid,
                    block_type="heading",
                )
            )
        s.commit()


def test_toc_titles_are_excluded_from_parsing_and_counted():
    units = [
        _unit("t1", "分类目录\nBPA ENCYCLOPEDIA"),
        _unit("t2", "未分类目录\nBPA ENCYCLOPEDIA"),
        _unit("t3", "目录"),
        _unit("a", "18根K线区间下方的突破失败：随后向上突破"),
        _unit("b", "无法识别的标题"),
    ]

    build = build_taxonomy_edges(units)

    assert build.stats["excluded_toc"] == 3
    assert build.stats["headings_loaded"] == 5
    assert build.stats["headings_in"] == 2
    assert build.stats["headings_after_dedupe"] == 2
    assert [(e.from_concept, e.to_concept) for e in build.edges] == [
        ("failed_breakout", "breakout")
    ]


def test_toc_rows_and_bodys_heading_with_colon_are_not_confused():
    # TOC rows are paragraph blocks in the real corpus and never load as
    # headings; a body heading that merely starts with a concept is parsed.
    body = parse_heading("分类目录下的突破失败：随后向上突破")

    assert is_toc_title("分类目录\nBPA ENCYCLOPEDIA") is True
    assert is_toc_title("分类目录下的突破失败：随后向上突破") is False
    assert body is not None and body.relation is Relation.TRANSITIONS_TO


def test_toc_paragraph_rows_never_enter_the_heading_scan(monkeypatch):
    Session = _memory_session(monkeypatch)
    _add_doc(Session, "enc", [("h1", "分类目录\nBPA ENCYCLOPEDIA")])
    with Session() as s:
        s.add(
            EvidenceUnitRow(
                evidence_id="row1",
                doc_id="enc",
                locator={"doc_id": "enc", "format": "pdf"},
                text="B18区间：向上突破",
                text_hash="row1",
                block_type="paragraph",
            )
        )
        s.commit()

    assert [u.evidence_id for u in taxonomy.load_heading_units(doc_ids=["enc"])] == ["h1"]
    stats = taxonomy.rebuild_taxonomy(doc_ids=["enc"])

    assert stats["excluded_toc"] == 1
    assert stats["headings_after_dedupe"] == 0
    assert stats["edges"] == 0
    with Session() as s:
        assert s.query(GraphEdgeRow).count() == 0


def test_rebuild_scopes_parsing_to_requested_documents(monkeypatch):
    Session = _memory_session(monkeypatch)
    _add_doc(Session, "enc", [("e1", "18根K线区间下方的突破失败：随后向上突破")])
    _add_doc(Session, "book", [("b1", "向下跳空：卖出高潮，但向上反转失败")])

    stats = taxonomy.rebuild_taxonomy(doc_ids=["enc"])

    assert stats["headings_in"] == 1
    assert stats["edges"] == 1
    with Session() as s:
        assert s.query(GraphEdgeRow).filter(GraphEdgeRow.origin == "heading_parse").count() == 1


def test_rebuild_replaces_every_heading_edge_regardless_of_relation(monkeypatch):
    Session = _memory_session(monkeypatch)
    _add_doc(Session, "enc", [("e1", "18根K线区间下方的突破失败：随后向上突破")])
    with Session() as s:
        s.add_all(
            [
                GraphEdgeRow(
                    edge_id="stale-heading",
                    from_node=concept_node_id("trend"),
                    to_node=concept_node_id("breakout"),
                    relation="SUPPORTED_BY",
                    source="old",
                    origin="heading_parse",
                ),
                GraphEdgeRow(
                    edge_id="seed-edge",
                    from_node=concept_node_id("trend"),
                    to_node=concept_node_id("trading_range"),
                    relation="TRANSITIONS_TO",
                    source="seed",
                    origin="seed",
                ),
            ]
        )
        s.commit()

    taxonomy.rebuild_taxonomy(doc_ids=["enc"])

    with Session() as s:
        assert s.query(GraphEdgeRow).filter(GraphEdgeRow.edge_id == "stale-heading").count() == 0
        assert s.query(GraphEdgeRow).filter(GraphEdgeRow.edge_id == "seed-edge").count() == 1
        assert s.query(GraphEdgeRow).filter(GraphEdgeRow.origin == "heading_parse").count() == 1


def test_neighbor_order_is_stable_when_endpoints_and_relation_match(monkeypatch):
    Session = _memory_session(monkeypatch)
    _add_doc(Session, "enc", [("e1", "18根K线区间下方的突破失败：随后向上突破")])
    with Session() as s:
        s.add_all(
            [
                GraphNodeRow(
                    node_id=concept_node_id("failed_breakout"),
                    node_type="concept",
                    label="失败突破",
                    concept_id="failed_breakout",
                ),
                GraphNodeRow(
                    node_id=concept_node_id("breakout"),
                    node_type="concept",
                    label="突破",
                    concept_id="breakout",
                ),
                GraphEdgeRow(
                    edge_id="seed-edge",
                    from_node=concept_node_id("failed_breakout"),
                    to_node=concept_node_id("breakout"),
                    relation="TRANSITIONS_TO",
                    source="seed",
                    origin="seed",
                ),
                GraphEdgeRow(
                    edge_id="heading-edge",
                    from_node=concept_node_id("failed_breakout"),
                    to_node=concept_node_id("breakout"),
                    relation="TRANSITIONS_TO",
                    source="e1",
                    origin="heading_parse",
                ),
            ]
        )
        s.commit()

    first = taxonomy.taxonomy_neighbors(["failed_breakout"])
    second = taxonomy.taxonomy_neighbors(["failed_breakout"])

    assert first == second
    assert [n["origin"] for n in first] == ["heading_parse", "seed"]
