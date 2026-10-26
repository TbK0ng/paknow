"""Graph expansion: seeds, budgets, seen-evidence dedupe, non-citable partition,
new-concept gating, fail-open, and route vocabulary.

Assertions are on the public retrieval seam (``retrieve_evidence``), the pure
planner/renderer, and the router's published vocabulary. No internal call
counts and no model output text.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from paknow.graph import expansion
from paknow.retrieval import pipeline
from paknow.workflows import generate, router


def _unit(eid: str, *, text: str = "text", score: float = 1.0) -> dict:
    return {
        "evidence_id": eid,
        "doc_id": "d1",
        "block_type": "paragraph",
        "text": text,
        "chapter_path": ["ch"],
        "pdf_page": 1,
        "printed_page": None,
        "figure_label": None,
        "epub_href": None,
        "figure_ids": [],
        "provenance": "native_text",
        "score": score,
    }


def _candidate(
    eid: str,
    *,
    text: str = "graph text",
    source: str = "mechanism",
    relation: str = "SUPPORTED_BY",
    concept: str = "breakout",
) -> dict:
    return {
        "evidence_id": eid,
        "text": text,
        "chapter_path": ["第1章"],
        "pdf_page": 12,
        "printed_page": None,
        "source": source,
        "relation": relation,
        "from_label": "突破",
        "to_label": "失败突破",
        "claim": {"subject": "突破", "predicate": "常常失败"},
        "origin": "claim_extract",
        "concept": concept,
    }


def _patched(
    monkeypatch,
    *,
    hits_by_query,
    fail_queries=(),
    mechanism=(),
    taxonomy=(),
    real_expansion=False,
):
    def fake_search(q, *, top_k=40):
        if q in fail_queries:
            raise RuntimeError("index offline")
        return [SimpleNamespace(payload=doc, score=doc.get("score", 1.0)) for doc in hits_by_query.get(q, [])]

    monkeypatch.setattr(pipeline, "hybrid_search", fake_search)
    monkeypatch.setattr(pipeline, "point_to_doc", lambda p: p.payload)
    monkeypatch.setattr(pipeline, "rerank", lambda q, docs, top_k=None: docs)
    monkeypatch.setattr(
        pipeline,
        "expand_evidence",
        lambda ids, **kwargs: {"units": [], "figures": []},
    )
    monkeypatch.setattr(pipeline, "toc_recall", lambda q: [])
    monkeypatch.setattr(pipeline, "barref_lookup", lambda q: [])
    if not real_expansion:
        monkeypatch.setattr(expansion, "load_mechanism_candidates", lambda *a, **k: list(mechanism))
        monkeypatch.setattr(expansion, "load_taxonomy_candidates", lambda *a, **k: list(taxonomy))


# ---------------------------------------------------------------------------
# seeds
# ---------------------------------------------------------------------------


def test_seed_concepts_follow_text_order_dedupe_and_cap():
    seeds = expansion.seed_concepts("突破 缺口 反转 突破 高潮 通道 楔形 交易区间 趋势 双顶 双底")

    assert seeds[:4] == ["breakout", "gap", "reversal", "climax"]
    assert len(seeds) == expansion.MAX_SEED_CONCEPTS
    assert len(set(seeds)) == len(seeds)


# ---------------------------------------------------------------------------
# pure planner: priority, budgets, dedupe
# ---------------------------------------------------------------------------


def test_mechanism_before_taxonomy_and_seen_evidence_is_marked():
    plan = expansion.plan_context(
        mechanism=[_candidate("e1"), _candidate("e2")],
        taxonomy=[_candidate("e1", source="taxonomy"), _candidate("e3", source="taxonomy")],
        seen_index={"e1": 2},
        max_evidence=2,
    )

    assert [e["evidence_id"] for e in plan.entries] == ["e1", "e2"]
    first = plan.entries[0]
    assert first["seen_index"] == 2 and first["text"] == ""
    assert first["provenance"] == "graph" and first["non_citable"] is True
    assert all(e["provenance"] == "graph" for e in plan.entries)
    assert plan.stats["truncated_evidence"] is True


def test_edge_budget_truncates_after_priority_order():
    plan = expansion.plan_context(
        mechanism=[_candidate(f"m{i}") for i in range(5)],
        taxonomy=[_candidate(f"t{i}", source="taxonomy") for i in range(5)],
        max_edges=3,
        max_evidence=8,
    )

    assert [e["evidence_id"] for e in plan.entries] == ["m0", "m1", "m2"]
    assert plan.stats["truncated_edges"] is True


def test_token_budget_drops_lower_priority_taxonomy_not_mechanism():
    long_text = "走" * 200  # ~200 estimated tokens
    plan = expansion.plan_context(
        mechanism=[_candidate("m0", text="short")],
        taxonomy=[_candidate("t0", text=long_text, source="taxonomy")],
        max_tokens=60,
    )

    assert [e["evidence_id"] for e in plan.entries] == ["m0"]
    assert plan.stats["truncated_tokens"] is True


# ---------------------------------------------------------------------------
# seam: expansion is a non-citable partition, seeded before/after text recall
# ---------------------------------------------------------------------------


def test_query_seed_expands_when_text_recall_returns_nothing(monkeypatch):
    _patched(
        monkeypatch,
        hits_by_query={},
        mechanism=[_candidate("g1")],
    )

    result = pipeline.retrieve_evidence("突破 后面会怎样", ["突破 后面会怎样"])

    assert result.text_hits == []
    assert [e["evidence_id"] for e in result.graph_evidence] == ["g1"]
    assert result.graph_evidence[0]["non_citable"] is True
    assert result.expanded_concepts == ["breakout"]


def test_graph_evidence_already_in_text_is_referenced_not_duplicated(monkeypatch):
    _patched(
        monkeypatch,
        hits_by_query={"突破": [_unit("e1", text="原文突破段落")]},
        mechanism=[_candidate("e1", text="重复正文"), _candidate("g2")],
    )

    result = pipeline.retrieve_evidence("突破", ["突破"])

    assert [h["evidence_id"] for h in result.text_hits] == ["e1"]
    entries = {e["evidence_id"]: e for e in result.graph_evidence}
    assert entries["e1"]["text"] == "" and entries["e1"]["seen_index"] == 0
    assert entries["g2"]["text"]
    # graph units never join the citable text partition
    assert {h["evidence_id"] for h in result.text_hits} == {"e1"}


def test_graph_evidence_never_enters_citation_indices(monkeypatch):
    monkeypatch.setattr(
        generate.gateway,
        "chat_json",
        lambda model, messages, **kwargs: generate.Answer(answer="答", cited_indices=[0]),
    )

    answer = generate.generate(
        "问题",
        [_unit("text-1", text="原文")],
        graph_evidence=[
            {"evidence_id": "graph-only", "source": "mechanism", "text": "图谱", "seen_index": None}
        ],
    )

    assert answer["evidence_ids"] == ["text-1"]


def test_expansion_failure_is_recorded_and_answer_continues(monkeypatch):
    _patched(monkeypatch, hits_by_query={"突破": [_unit("e1")]})

    def boom(*args, **kwargs):
        raise RuntimeError("edge table locked")

    monkeypatch.setattr(expansion, "load_mechanism_candidates", boom)
    monkeypatch.setattr(expansion, "load_taxonomy_candidates", boom)

    result = pipeline.retrieve_evidence("突破", ["突破"])

    assert [h["evidence_id"] for h in result.text_hits] == ["e1"]
    assert any("edge table locked" in f for f in result.failures)
    assert result.graph_evidence == []


# ---------------------------------------------------------------------------
# new-concept gating across rounds
# ---------------------------------------------------------------------------


def test_expansion_skips_round_without_new_concepts(monkeypatch):
    _patched(
        monkeypatch,
        hits_by_query={"突破": [_unit("e1")]},
        mechanism=[_candidate("g1")],
    )

    first = pipeline.retrieve_evidence("突破", ["突破"])
    again = pipeline.retrieve_evidence(
        "突破", ["突破"], acc=first.acc, expanded_concepts=first.expanded_concepts
    )

    assert [e["evidence_id"] for e in first.graph_evidence] == ["g1"]
    assert again.graph_evidence == []
    assert again.expanded_concepts == first.expanded_concepts


def test_gap_round_with_a_new_concept_expands_incrementally(monkeypatch):
    _patched(
        monkeypatch,
        hits_by_query={"缺口": [_unit("e2")]},
        mechanism=[_candidate("g2", concept="gap")],
    )

    result = pipeline.retrieve_evidence(
        "缺口 后续",
        ["缺口 后续"],
        acc={"e1": _unit("e1")},
        expanded_concepts=["breakout"],
    )

    assert [e["evidence_id"] for e in result.graph_evidence] == ["g2"]
    assert result.expanded_concepts == ["breakout", "gap"]


# ---------------------------------------------------------------------------
# prompt rendering + route vocabulary
# ---------------------------------------------------------------------------


def test_graph_context_renders_compact_non_citable_lines_with_evidence_ids():
    lines = generate.render_graph_context(
        [
            {
                "evidence_id": "g1",
                "source": "mechanism",
                "relation": "SUPPORTED_BY",
                "from_label": "突破",
                "to_label": "失败突破",
                "claim": {"subject": "突破", "predicate": "常常失败"},
                "chapter_path": ["第1章"],
                "pdf_page": 12,
                "epub_href": "OEBPS/ch_05.htm",
                "text": "图谱正文",
                "seen_index": None,
            },
            {
                "evidence_id": "text-1",
                "source": "taxonomy",
                "relation": "INVALIDATED_BY",
                "from_label": "突破",
                "to_label": "失败突破",
                "chapter_path": ["第1章"],
                "pdf_page": 3,
                "text": "",
                "seen_index": 2,
            },
        ]
    )

    assert len(lines) == 2
    assert all("不可引用" in line for line in lines)
    assert "g1" in lines[0] and "图谱正文" in lines[0]
    assert "EPUB ch_05.htm" in lines[0]
    assert "text-1" in lines[1] and "已见 [2]" in lines[1]


def test_router_vocabulary_drops_graph_local(monkeypatch):
    monkeypatch.setattr(
        router.gateway,
        "chat_json",
        lambda model, messages, **kwargs: router.QueryPlan(
            routes=["hybrid_text", "graph_local", "toc"]
        ),
    )

    plan = router.route("突破")

    assert plan.routes == ["hybrid_text", "toc"]
    assert "graph_local" not in router.ROUTE_VOCAB


# ---------------------------------------------------------------------------
# DB-backed hydration (no embeddings involved)
# ---------------------------------------------------------------------------


def _graph_db(monkeypatch, *, extra_evidence=()):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from paknow.db import Base
    from paknow.db.models import (
        ClaimRow,
        DocumentRow,
        EvidenceUnitRow,
        GraphEdgeRow,
        GraphNodeRow,
    )
    from paknow.graph.identity import concept_node_id

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(expansion, "SessionLocal", Session)

    with Session() as s:
        s.add(DocumentRow(doc_id="d1", content_hash="h", format="pdf", source_path="x"))
        s.add_all(
            [
                EvidenceUnitRow(
                    evidence_id="h1",
                    doc_id="d1",
                    locator={"doc_id": "d1", "format": "pdf", "printed_page_label": "12"},
                    text="18根K线区间下方的突破失败：随后向上突破",
                    text_hash="h1",
                    block_type="heading",
                    chapter_path=["第1章"],
                    pdf_page_1based=7,
                ),
                EvidenceUnitRow(
                    evidence_id="m1",
                    doc_id="d1",
                    locator={"doc_id": "d1", "format": "pdf"},
                    text="突破常常在区间边缘失败。",
                    text_hash="m1",
                    block_type="paragraph",
                    chapter_path=["第2章"],
                    pdf_page_1based=9,
                ),
                *[
                    EvidenceUnitRow(
                        evidence_id=eid,
                        doc_id="d1",
                        locator={"doc_id": "d1", "format": "pdf"},
                        text=text,
                        text_hash=eid,
                        block_type="paragraph",
                    )
                    for eid, text in extra_evidence
                ],
            ]
        )
        s.add_all(
            [
                GraphNodeRow(
                    node_id=concept_node_id("breakout"),
                    node_type="concept",
                    label="突破",
                    concept_id="breakout",
                ),
                GraphNodeRow(
                    node_id=concept_node_id("failed_breakout"),
                    node_type="concept",
                    label="失败突破",
                    concept_id="failed_breakout",
                ),
                GraphEdgeRow(
                    edge_id="edge-1",
                    from_node=concept_node_id("failed_breakout"),
                    to_node=concept_node_id("breakout"),
                    relation="TRANSITIONS_TO",
                    source="h1",
                    origin="heading_parse",
                ),
                ClaimRow(
                    claim_id="c1",
                    subject="突破",
                    subject_concept_id="breakout",
                    predicate="常常在区间边缘失败",
                    qualifiers={"modality": "often"},
                    supporting_evidence=["m1"],
                ),
            ]
        )
        s.commit()
    return Session


def test_db_hydration_returns_locators_and_edge_provenance(monkeypatch):
    _graph_db(monkeypatch)

    taxonomy = expansion.load_taxonomy_candidates(["breakout"])
    mechanism = expansion.load_mechanism_candidates(["breakout"])

    assert taxonomy[0]["evidence_id"] == "h1"
    assert taxonomy[0]["relation"] == "TRANSITIONS_TO"
    assert taxonomy[0]["chapter_path"] == ["第1章"]
    assert taxonomy[0]["pdf_page"] == 7 and taxonomy[0]["printed_page"] == "12"
    assert taxonomy[0]["origin"] == "heading_parse"

    assert mechanism[0]["evidence_id"] == "m1"
    assert mechanism[0]["claim"]["subject"] == "突破"
    assert mechanism[0]["pdf_page"] == 9


def test_seam_with_real_hydration_dedupes_seen_evidence(monkeypatch):
    _graph_db(monkeypatch)
    _patched(
        monkeypatch,
        hits_by_query={"突破": [_unit("m1", text="突破常常在区间边缘失败。")]},
        real_expansion=True,
    )

    result = pipeline.retrieve_evidence("突破", ["突破"])

    ids = [e["evidence_id"] for e in result.graph_evidence]
    # mechanism chain outranks the taxonomy edge; both hydrate from the DB
    assert ids == ["m1", "h1"]
    assert result.graph_evidence[0]["seen_index"] == 0
    assert result.graph_evidence[0]["text"] == ""
    assert result.text_hits[0]["evidence_id"] == "m1"


def test_graph_evidence_budget_is_14_and_the_other_caps_stay():
    assert expansion.MAX_EVIDENCE == 14
    assert expansion.MAX_CONTEXT_TOKENS == 1200
    assert expansion.MAX_EDGES == 32

    plan = expansion.plan_context(
        mechanism=[_candidate(f"g{i}") for i in range(20)],
        taxonomy=[],
    )

    assert plan.stats["selected"] == 14
    assert plan.stats["truncated_evidence"] is True


def test_default_graph_block_budget_is_1200_estimated_tokens():
    assert expansion.MAX_CONTEXT_TOKENS == 1200
    plan = expansion.plan_context(
        mechanism=[_candidate(f"m{i}", text="突" * 200) for i in range(6)],
        taxonomy=[_candidate("t0", text="分类", source="taxonomy")],
    )

    assert plan.stats["truncated_tokens"] is True
    assert plan.stats["tokens"] <= expansion.MAX_CONTEXT_TOKENS
    assert 0 < plan.stats["selected"] < 7
    assert all(e["source"] == "mechanism" for e in plan.entries)


def test_seam_exposes_graph_budget_stats_for_evaluation(monkeypatch):
    _patched(
        monkeypatch,
        hits_by_query={},
        mechanism=[_candidate(f"g{i}") for i in range(expansion.MAX_EVIDENCE + 2)],
    )

    result = pipeline.retrieve_evidence("突破", ["突破"])

    assert result.graph_stats["selected"] == expansion.MAX_EVIDENCE
    assert result.graph_stats["truncated_evidence"] is True
    assert result.graph_stats["seeds"] == 1
    assert len(result.graph_evidence) == result.graph_stats["selected"]


def test_gap_query_terms_seed_expansion_too(monkeypatch):
    _patched(monkeypatch, hits_by_query={}, mechanism=[_candidate("g9")])

    result = pipeline.retrieve_evidence(
        "继续说明", ["继续说明", "突破的后续"], acc={"old": _unit("old")}
    )

    assert [e["evidence_id"] for e in result.graph_evidence] == ["g9"]


def test_text_hit_concepts_seed_second_pass(monkeypatch):
    _patched(
        monkeypatch,
        hits_by_query={"后续如何": [_unit("e1", text="突破常常在区间边缘失败")]},
        mechanism=[_candidate("g5")],
    )

    result = pipeline.retrieve_evidence("后续如何", ["后续如何"])

    # the query mentions no concept; the hit-evidence text supplies the seed
    assert result.expanded_concepts == ["breakout"]
    assert [e["evidence_id"] for e in result.graph_evidence] == ["g5"]


# ---------------------------------------------------------------------------
# edge budget: counted per traversed candidate, not per selected entry
# ---------------------------------------------------------------------------


def test_edge_budget_binds_on_traversed_candidates():
    # 40 candidate edges over only 5 distinct evidence ids: the evidence cap
    # never binds, so the edge budget must stop the walk
    plan = expansion.plan_context(
        mechanism=[_candidate(f"e{i % 5}") for i in range(40)],
        taxonomy=[],
    )

    assert plan.stats["truncated_edges"] is True
    assert plan.stats["truncated_evidence"] is False
    assert plan.stats["candidates"] == expansion.MAX_EDGES + 1
    assert len(plan.entries) == 5


def test_edge_budget_keeps_mechanism_before_taxonomy_at_the_boundary():
    plan = expansion.plan_context(
        mechanism=[_candidate(f"m{i}") for i in range(4)],
        taxonomy=[_candidate(f"t{i}", source="taxonomy") for i in range(4)],
        max_edges=4,
    )

    assert [e["evidence_id"] for e in plan.entries] == ["m0", "m1", "m2", "m3"]
    assert plan.stats["truncated_edges"] is True


# ---------------------------------------------------------------------------
# conditioned claims stay conditioned in the prompt
# ---------------------------------------------------------------------------


def test_claim_conditions_maps_the_stored_qualifier_keys():
    assert (
        generate._claim_conditions({"context": ["强趋势", "缩量"], "modality": "may"})
        == "背景=强趋势、缩量；语气=may"
    )
    assert generate._claim_conditions({}) == ""


def test_conditioned_claim_renders_condition_and_claim_on_one_line():
    line = generate.render_graph_context(
        [
            {
                "evidence_id": "g1",
                "source": "mechanism",
                "claim": {
                    "subject": "突破",
                    "predicate": "常常失败",
                    "qualifiers": {"context": ["强趋势"], "modality": "usually"},
                },
                "text": "图谱正文",
                "seen_index": None,
            }
        ]
    )[0]

    assert "突破 → 常常失败" in line
    assert "条件：背景=强趋势；语气=usually" in line


def test_unconditional_claim_renders_without_condition_wording():
    line = generate.render_graph_context(
        [
            {
                "evidence_id": "g1",
                "source": "mechanism",
                "claim": {"subject": "突破", "predicate": "常常失败", "qualifiers": {}},
                "text": "图谱正文",
                "seen_index": None,
            }
        ]
    )[0]

    assert "突破 → 常常失败" in line
    assert "条件" not in line
    assert "无条件" not in line
    assert "通常在" not in line


# ---------------------------------------------------------------------------
# gap-round accumulation
# ---------------------------------------------------------------------------


def test_gap_round_appends_new_entries_to_prior_graph_evidence(monkeypatch):
    _patched(monkeypatch, hits_by_query={}, mechanism=[_candidate("g2", concept="gap")])

    result = pipeline.retrieve_evidence(
        "缺口 后续",
        ["缺口 后续"],
        acc={"e1": _unit("e1")},
        expanded_concepts=["breakout"],
        prior_graph_evidence=[_candidate("g1")],
    )

    assert [e["evidence_id"] for e in result.graph_evidence] == ["g1", "g2"]
    assert result.graph_stats["seeds"] == 1
    assert result.graph_stats["accumulated"] == 2


def test_gap_round_dedupes_by_evidence_id_and_keeps_prior_body(monkeypatch):
    _patched(
        monkeypatch,
        hits_by_query={},
        mechanism=[_candidate("g1", text="this-round text"), _candidate("g2", concept="gap")],
    )

    result = pipeline.retrieve_evidence(
        "缺口 后续",
        ["缺口 后续"],
        expanded_concepts=["breakout"],
        prior_graph_evidence=[_candidate("g1", text="prior text")],
    )

    assert [e["evidence_id"] for e in result.graph_evidence] == ["g1", "g2"]
    assert result.graph_evidence[0]["text"] == "prior text"


def test_round_without_new_concepts_preserves_prior_evidence_and_reports_stats(monkeypatch):
    _patched(monkeypatch, hits_by_query={})

    result = pipeline.retrieve_evidence(
        "突破",
        ["突破"],
        expanded_concepts=["breakout"],
        prior_graph_evidence=[_candidate("g1")],
    )

    assert [e["evidence_id"] for e in result.graph_evidence] == ["g1"]
    assert result.graph_stats["seeds"] == 0
    assert result.graph_stats["selected"] == 1
    assert result.graph_stats["truncated_edges"] is False


# ---------------------------------------------------------------------------
# one citation-binding rule shared by the pipeline and generation
# ---------------------------------------------------------------------------


def test_pipeline_binding_skips_derived_observations_and_dedupes():
    units = [
        _unit("e1"),
        {"evidence_id": "obs-1", "provenance": "derived_observation", "text": "观察"},
        _unit("p1"),
    ]

    assert pipeline.citable_units([_unit("e1")], [_unit("e1"), _unit("p1")]) == [
        units[0],
        units[2],
    ]
    assert pipeline.citation_binding(units) == {"e1": 0, "p1": 1}


def test_generation_uses_the_pipeline_citation_indices(monkeypatch):
    units = [
        _unit("e1", text="原文一"),
        {"evidence_id": "obs-1", "provenance": "derived_observation", "text": "观察"},
        _unit("p1", text="原文二"),
    ]
    index = pipeline.citation_binding(units)
    captured = {}

    def fake_chat_json(schema, messages, **kwargs):
        captured["user"] = messages[-1]["content"]
        return generate.Answer(answer="答", cited_indices=[1])

    monkeypatch.setattr(generate.gateway, "chat_json", fake_chat_json)

    answer = generate.generate("问题", units, citation_index=index)

    assert answer["evidence_ids"] == ["p1"]
    assert "[1]" in captured["user"]
    assert "原文二" in captured["user"].split("[1]", 1)[1]


def test_router_falls_back_to_hybrid_text_when_filtering_empties_routes(monkeypatch):
    monkeypatch.setattr(
        router.gateway,
        "chat_json",
        lambda model, messages, **kwargs: router.QueryPlan(routes=["graph_local"]),
    )

    assert router.route("突破").routes == ["hybrid_text"]


def test_retrieve_node_hands_prior_graph_evidence_back_and_keeps_it(monkeypatch):
    from paknow.workflows import graph as graph_flow

    captured = {}

    def fake_retrieve(query, queries, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            acc={},
            text_hits=[],
            figures=[],
            context_units=[],
            citable_units=[],
            citation_index={},
            toc_context=[],
            barrefs=[],
            graph_evidence=[
                *kwargs["prior_graph_evidence"],
                {"evidence_id": "g2", "non_citable": True},
            ],
            graph_stats={"seeds": 1, "selected": 2, "accumulated": 2},
            expanded_concepts=["breakout", "gap"],
            failures=[],
        )

    monkeypatch.setattr(graph_flow, "retrieve_evidence", fake_retrieve)
    state = {
        "query": "缺口 后续",
        "plan": router.QueryPlan(),
        "graph_evidence": [{"evidence_id": "g1"}],
        "expanded_concepts": ["breakout"],
        "acc": {},
    }

    out = graph_flow._retrieve_node(state)

    assert captured["prior_graph_evidence"] == [{"evidence_id": "g1"}]
    assert [e["evidence_id"] for e in out["graph_evidence"]] == ["g1", "g2"]
    assert out["graph_stats"]["accumulated"] == 2
