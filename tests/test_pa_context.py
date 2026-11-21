"""The checked PA graph depends on causal setup tags and honest passage identity."""
from dataclasses import replace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from paknow.db import Base
from paknow.db.models import ClaimRow, DocumentRow, EvidenceUnitRow, GraphEdgeRow
from paknow.graph import identity, pa_context
from paknow.pa_rules import STEP, Bar
from paknow.retrieval.dictionary import Term
from paknow.retrieval.pa_corpus import evidence_digest, graph_digest


@pytest.mark.parametrize("side,forward,opposite,outside", [
    ("long", "upper", "lower", "below"), ("short", "lower", "upper", "above"),
])
def test_zone_profile_distinguishes_edge_reentry_and_opposite_boundary(side, forward, opposite, outside):
    s = pa_context.Setup(side, "range_or_mixed", "mixed", forward, "ema_proxy", "same_side_close", 1, 1,
                         True, False, False, (), False)
    profiles = pa_context._profiles(s)
    assert profiles[:3] == ("edge", "proxy", "obstacle")
    assert "reentry" not in profiles and "range_fade" not in profiles
    assert pa_context._profiles(replace(s, reentered=True))[0] == "reentry"
    assert "range_fade" in pa_context._profiles(replace(s, zone=opposite))
    assert "range_fade" not in pa_context._profiles(replace(s, zone=outside))
    assert "repeated" not in pa_context._profiles(replace(s, test="observed_by_rule"))
    assert "repeated" in pa_context._profiles(replace(s, test="observed_by_rule", repeated_with_reaction=True))


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("excursion,close,expected", [
    (0, -4, False), (2, 0, False), (2, -4, True), (2, 1, False), (2, -11, False),
])
def test_signal_boundary_predicate_is_causal_and_mirrored(side, excursion, close, expected):
    sign = 1 if side == "long" else -1

    def price(distance):
        return 100 + sign * distance

    def bar(i, extreme, final):
        low, high = sorted((price(min(-10, final)), price(extreme)))
        return Bar(i * STEP, price(-6), high, low, price(final), 1)

    bars = (bar(1, 0, -6), *(bar(i, -5, -7) for i in range(2, 81)), bar(81, excursion, close))
    state = {"original_state": side, "close_ms": 82 * STEP, "background": {"regime": "range_or_mixed"},
             "plan": {"alignment": "mixed", "edge_before_target": None},
             side: {"checks": [{"id": "trigger", "passed": True}], "test_bars": [], "ema_proxy": True}}
    setup = pa_context.setup_at(state, bars)
    assert setup.edge == 100 and setup.reentered is expected
    # Only the selected signal close, not later follow-through.
    assert setup.signal == ("prior_extreme_close" if close > -5 else "same_side_close")
    assert ("reentry" in pa_context._profiles(setup)) is expected
    with pytest.raises(ValueError, match="exactly 80"):
        pa_context.setup_at(state, (*bars, bar(82, 3, 1)))


def test_canonical_id_cannot_be_shadowed_or_depend_on_ambiguous_alias_order(monkeypatch):
    rows = [Term("second_entry", ["high_2", "ambiguous"]), Term("high_2", ["高二", "ambiguous"])]
    monkeypatch.setattr(identity, "load_terms", lambda: rows)
    try:
        for _ in range(2):
            identity._index.cache_clear()
            assert identity.canonical_for("HIGH_2") == identity.canonical_for("高二") == "high_2"
            assert identity.canonical_for("ambiguous") is None and identity.canonical_for("unknown") is None
            rows.reverse()
        lead = pa_context.Lead("support", "high_2", "question", "e", "sha", "REQUIRES_CONTEXT", "repeated")
        key = identity.concept_node_id(lead.reason()["concept_id"])
        next(term for term in rows if term.canonical == "high_2").aliases = ["高二新写法"]
        identity._index.cache_clear()
        assert lead.reason()["concept"] == "高二新写法"
        assert identity.concept_node_id(lead.reason()["concept_id"]) == key
    finally:
        identity._index.cache_clear()


@pytest.fixture
def reviewed_graph(tmp_path, monkeypatch):
    from hashlib import sha256

    import yaml
    from paknow.retrieval.pa_corpus import PA_GRAPH_SOURCES
    from scripts.rebuild_text_graph import seed_passages

    engine = create_engine("sqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        sessions = sessionmaker(engine)
        monkeypatch.setattr(pa_context, "SessionLocal", sessions)
        # Test actual curator metadata, with local stand-ins for the six originals.
        entries = yaml.safe_load(PA_GRAPH_SOURCES.read_text(encoding="utf-8"))
        with sessions.begin() as db:
            for entry in entries:
                eid = entry["evidence"]
                text = f"Unrelated synthetic passage for test anchor {eid}."
                entry["sha256"] = sha256(text.encode()).hexdigest()
                db.merge(EvidenceUnitRow(evidence_id=eid, doc_id="doc", locator={}, text=text,
                                         text_hash=entry["sha256"], provenance="native_text"))

        source_path = tmp_path / "reviewed.yaml"
        source_path.write_text(yaml.safe_dump(entries), encoding="utf-8")
        assert seed_passages(source_path, session_factory=sessions) == len(entries)
        yield sessions
    finally:
        engine.dispose()


@pytest.mark.parametrize("side,zone,proxy,failed,opposite", [
    ("long", "upper", "1c84d128cd372ffcdd55a321da1563e1", "56cdcd45aec67f0451d8b1312c422903", "lower"),
    ("short", "lower", "1ef780d59a611fcc2238a13aa9a84a52", "2ba015dbeb9b0903c7b7dde47c6ed029", "upper"),
])
def test_planner_preserves_distinct_arriving_conditions(reviewed_graph, side, zone, proxy, failed, opposite):
    s = pa_context.Setup(side, "range_or_mixed", "mixed", zone, "ema_proxy", "same_side_close", 1, 1,
                         True, True, False, (), False)
    risk = [lead for lead in pa_context.plan(s) if lead.role == "risk"]
    assert risk[0].evidence_id == failed and risk[0].relation == "INVALIDATED_BY"
    assert risk[1].evidence_id == proxy
    if side == "short":
        assert risk[2].profile == "obstacle"
    assert "目标之前" in pa_context.base_questions(s)["risk"]
    for forward in (zone, "above" if side == "long" else "below"):
        risks = [lead for lead in pa_context.plan(replace(s, zone=forward, reentered=False)) if lead.role == "risk"]
        assert risks[0].profile == "edge" and risks[0].relation == "EXCEPTION_TO"
        assert all(lead.relation != "INVALIDATED_BY" for lead in risks)
    fading = replace(s, zone=opposite, reentered=False, test="observed_by_rule")
    repeated = replace(fading, repeated_with_reaction=True)
    assert [lead.profile for lead in pa_context.plan(fading) if lead.role == "support"] == ["range_fade", "mixed"]
    assert [lead.profile for lead in pa_context.plan(repeated) if lead.role == "support"] == ["range_fade", "repeated"]
    assert "中间曾出现反应" in pa_context.base_questions(repeated)["support"]


def test_graph_seal_binds_review_status_and_both_runtime_edges():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory.begin() as db:
        claim = ClaimRow(claim_id="c", subject="entry", subject_concept_id="second_entry", predicate="q",
                         qualifiers={"role": "support"}, supporting_evidence=["e"],
                         source_scope=pa_context.SCOPE, extraction_status="reviewed")
        edge = GraphEdgeRow(edge_id="link", from_node="c", to_node="e", relation="SUPPORTED_BY",
                            source="e", origin=pa_context.SCOPE, extraction_status="reviewed")
        db.add_all([claim, edge])
        db.flush()
        original = graph_digest(db)
        claim.extraction_status = "pending"
        db.flush()
        assert graph_digest(db) != original
        claim.extraction_status = "reviewed"
        db.flush()
        assert graph_digest(db) == original
        edge.extraction_status = "pending"
        db.flush()
        assert graph_digest(db) != original
        db.delete(edge)
        db.flush()
        assert graph_digest(db) != original
    engine.dispose()


@pytest.mark.parametrize("change", [{"text": "changed"}, {"locator": {"pdf_page_1based": 42}}, {"parent_id": "other"}])
def test_evidence_seal_binds_text_location_and_parent(change):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory.begin() as db:
        db.add(DocumentRow(doc_id="doc", content_hash="hash", format="pdf", source_path="book.pdf"))
        row = EvidenceUnitRow(evidence_id="x", doc_id="doc", locator={"pdf_page_1based": 1}, text="original", text_hash="hash")
        db.add(row)
        db.flush()
        original = evidence_digest(db)
        for key, value in change.items():
            setattr(row, key, value)
        db.flush()
        assert evidence_digest(db) != original
    engine.dispose()
