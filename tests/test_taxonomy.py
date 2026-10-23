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
from paknow.graph.taxonomy import HeadingUnit, build_taxonomy_edges, dedupe_headings, parse_heading


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
