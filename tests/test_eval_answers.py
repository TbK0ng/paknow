"""Answer-level runner: arm passthrough, checkpoint merge, hard constraints and
summary arithmetic. No real model and no Qdrant: every chain step is a stub.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from paknow.retrieval.pipeline import RetrievalResult, citation_binding
from paknow.workflows import graph as graph_module
from paknow.workflows.router import QueryPlan, retrieval_queries
from scripts import eval_answers


# ---------------------------------------------------------------------------
# run_query passthrough
# ---------------------------------------------------------------------------


def _stub_chain(monkeypatch, *, units, citation_index, context_units=()):
    """Replace every model/DB step of the production chain with a stub."""
    calls: list[dict] = []

    def fake_retrieve(query, queries, **kwargs):
        calls.append({"query": query, "queries": list(queries), **kwargs})
        return RetrievalResult(
            text_hits=[u for u in units if u.get("provenance") != "derived_observation"],
            context_units=list(context_units),
            citable_units=list(units),
            citation_index=dict(citation_index),
            acc={},
            graph_stats={"enabled": kwargs.get("expand_graph", True)},
            failures=[],
        )

    monkeypatch.setattr(graph_module, "retrieve_evidence", fake_retrieve)
    monkeypatch.setattr(
        graph_module,
        "route",
        lambda query, images=None: QueryPlan(
            intent="knowledge_qa",
            routes=["hybrid_text"],
            evidence_needs=["definition"],
            sub_queries=["sub"],
            max_search_rounds=3,
        ),
    )
    monkeypatch.setattr(graph_module, "select_sop", lambda intent: None)
    monkeypatch.setattr(
        graph_module,
        "check_sufficiency",
        lambda *a, **k: (
            SimpleNamespace(
                next_query=None,
                model_dump=lambda: {"sufficient": True, "next_query": None},
            ),
            None,
        ),
    )
    monkeypatch.setattr(
        graph_module,
        "generate",
        lambda *a, **k: {"answer": "answer", "evidence_ids": ["e1"], "figure_ids": []},
    )
    monkeypatch.setattr(graph_module, "save_trace", lambda *a, **k: None)
    monkeypatch.setattr(graph_module, "_maybe_auto_optimize", lambda: None)
    return calls


def test_run_query_flag_reaches_the_retrieval_seam_once_per_arm(monkeypatch):
    units = [{"evidence_id": "e1", "text": "a", "provenance": "text"}]
    calls = _stub_chain(monkeypatch, units=units, citation_index={"e1": 0})

    graph_module.run_query("q", expand_graph=False)
    assert len(calls) == 1
    assert calls[0]["expand_graph"] is False

    graph_module.run_query("q")
    assert len(calls) == 2
    assert calls[1]["expand_graph"] is True


def test_run_query_exposes_citable_ids_without_derived_observations(monkeypatch):
    units = [
        {"evidence_id": "e1", "text": "a", "provenance": "text"},
        {"evidence_id": "e2", "text": "visual", "provenance": "derived_observation"},
        {"evidence_id": "e3", "text": "context", "provenance": "context"},
    ]
    index = citation_binding(units)
    _stub_chain(monkeypatch, units=units, citation_index=index)

    result = graph_module.run_query("q")

    assert result["citable_ids"] == ["e1", "e3"]
    assert "e2" not in result["citable_ids"]
    assert result["rounds"] == 1
    assert result["failures"] == []


def test_run_query_exposes_the_verdict_and_context_unit_counts(monkeypatch):
    units = [
        {"evidence_id": "e1", "text": "a", "provenance": "text"},
        {"evidence_id": "e2", "text": "visual", "provenance": "derived_observation"},
    ]
    context_units = [
        {"evidence_id": "p1", "text": "父块", "provenance": "native_text", "is_parent": True},
        {"evidence_id": "n1", "text": "邻接", "provenance": "native_text", "is_adjacent": True},
        {"evidence_id": "n2", "text": "邻接二", "provenance": "native_text", "is_adjacent": True},
    ]
    _stub_chain(
        monkeypatch,
        units=units,
        citation_index=citation_binding(units),
        context_units=context_units,
    )

    result = graph_module.run_query("q")

    assert result["sufficiency"] == {"sufficient": True, "next_query": None}
    assert result["context_unit_counts"] == {"total": 3, "parents": 1, "adjacent": 2}
    # the debug view reports the same binding generation uses
    assert result["citable_ids"] == ["e1"]
    assert "e2" not in result["citable_ids"]


# ---------------------------------------------------------------------------
# deterministic constraints
# ---------------------------------------------------------------------------


def test_citation_checks_pass_when_all_cited_ids_are_citable_and_not_graph():
    checks = eval_answers.citation_checks(["e1", "e2"], {"e1", "e2", "e3"}, {"g1"})

    assert checks == {
        "invalid_citations": [],
        "graph_citations": [],
        "citations_valid": True,
        "no_graph_citations": True,
        "hard_pass": True,
    }


def test_citation_checks_allow_a_graph_entry_that_is_also_text_citable():
    # the graph block points at a text unit too ("已见 [n]"): citing it cites
    # the author's text, not graph synthesis
    checks = eval_answers.citation_checks(["e1"], {"e1"}, {"e1", "g1"})

    assert checks["graph_citations"] == []
    assert checks["hard_pass"] is True


def test_refresh_checks_recomputes_from_stored_ids():
    record = {
        "id": "q1",
        "arm": "graph",
        "status": "ok",
        "cited_evidence_ids": ["g1"],
        "citable_ids": ["e1"],
        "graph_evidence_ids": ["g1"],
        "expected_evidence": ["e1"],
        "graph_citations": [],  # stale value from an older run
        "hard_pass": True,
    }

    refreshed = eval_answers.refresh_checks(record)

    assert refreshed["graph_citations"] == ["g1"]
    assert refreshed["hard_pass"] is False
    assert refreshed["coverage"] == 1.0


def test_citation_checks_fail_on_out_of_set_and_graph_citations():
    checks = eval_answers.citation_checks(["e1", "e9", "g1"], {"e1"}, {"g1"})

    assert checks["invalid_citations"] == ["e9", "g1"]
    assert checks["graph_citations"] == ["g1"]
    assert checks["citations_valid"] is False
    assert checks["no_graph_citations"] is False
    assert checks["hard_pass"] is False


def test_expected_coverage_passes_only_when_every_expected_unit_is_citable():
    full = eval_answers.expected_coverage(["e1", "e2"], {"e1", "e2", "e9"})
    partial = eval_answers.expected_coverage(["e1", "e2"], {"e1"})

    assert full["coverage"] == 1.0
    assert full["coverage_complete"] is True
    assert full["expected_missing"] == []
    assert partial["coverage"] == 0.5
    assert partial["coverage_complete"] is False
    assert partial["expected_missing"] == ["e2"]


def test_run_item_never_passes_expected_evidence_to_generation():
    seen: list[tuple] = []

    def fake_run_query(query, *, expand_graph, plan=None):
        seen.append((query, expand_graph, plan))
        return {
            "answer": {"answer": "a", "evidence_ids": ["e1"]},
            "citable_ids": ["e1"],
            "graph_evidence": [],
            "routes": ["hybrid_text"],
            "rounds": 2,
            "failures": [],
        }

    item = {
        "id": "q1",
        "category": "encyclopedia",
        "question": "问法",
        "expected_evidence": ["e1"],
        "expected_points": [{"point": "secret"}],
    }
    record = eval_answers.run_item(item, "text", fake_run_query)

    assert seen == [("问法", False, None)]
    assert record["status"] == "ok"
    assert record["hard_pass"] is True
    assert record["coverage"] == 1.0
    assert record["rounds"] == 2
    assert record["plan_source"] == "routed"


def test_run_item_records_a_failure_and_keeps_the_batch_alive():
    def boom(query, *, expand_graph, plan=None):
        raise RuntimeError("gateway down")

    item = {"id": "q1", "category": "mechanism", "question": "q", "expected_evidence": ["e1"]}
    record = eval_answers.run_item(item, "graph", boom, frozen=True)

    assert record["status"] == "error"
    assert record["arm"] == "graph"
    assert record["plan_source"] == "frozen"  # provenance survives the error
    assert record["queries"] == []
    assert record["query_hash"] == ""
    assert "gateway down" in record["error"]
    assert record["hard_pass"] is False
    assert record["expected_missing"] == ["e1"]


# ---------------------------------------------------------------------------
# frozen queries (--frozen-queries)
# ---------------------------------------------------------------------------


def _gold_item(item_id: str = "q1", *, category: str = "encyclopedia") -> dict:
    question = f"{item_id} 问法"
    return {
        "id": item_id,
        "category": category,
        "question": question,
        "expanded_queries": [
            question,
            f"{item_id}-2",
            f"{item_id}-3",
            f"{item_id}-4",
            f"{item_id}-5",
            f"{item_id}-6",
        ],
        "expected_evidence": ["e1"],
        "expected_points": [{"text": "secret", "evidence_id": "e1"}],
    }


def test_frozen_plan_reproduces_the_gold_expanded_queries():
    item = _gold_item()
    plan = eval_answers.frozen_plan(item)

    assert retrieval_queries(plan, item["question"]) == item["expanded_queries"]


def test_run_item_frozen_passes_the_plan_and_records_provenance():
    item = _gold_item()
    plans: list = []

    def fake_run_query(query, *, expand_graph, plan=None):
        plans.append(plan)
        return {
            "answer": {"answer": "a", "evidence_ids": ["e1"]},
            "citable_ids": ["e1"],
            "graph_evidence": [],
            "routes": ["hybrid_text"],
            "rounds": 1,
            "failures": [],
            "plan_source": "frozen",
            "queries": list(item["expanded_queries"]),
            "query_hash": graph_module.query_hash(item["expanded_queries"]),
        }

    record = eval_answers.run_item(item, "text", fake_run_query, frozen=True)

    assert plans[0].sub_queries == item["expanded_queries"][1:]
    assert record["plan_source"] == "frozen"
    assert record["queries"] == item["expanded_queries"]
    assert record["query_hash"] == graph_module.query_hash(item["expanded_queries"])


def test_run_item_keeps_the_degradation_marker_readable():
    item = _gold_item()
    degradation = {"reason": "timeout", "detail": "TimeoutError: slow"}

    def fake_run_query(query, *, expand_graph, plan=None):
        return {
            "answer": {
                "answer": "本次生成失败，请重试",
                "evidence_ids": [],
                "degraded": True,
                "degradation": dict(degradation),
            },
            "citable_ids": [],
            "graph_evidence": [],
            "routes": [],
            "rounds": 1,
            "failures": ["generate: timeout: TimeoutError: slow"],
        }

    record = eval_answers.run_item(item, "graph", fake_run_query)

    assert record["degraded"] is True
    assert record["degradation"] == degradation
    assert record["failures"] == ["generate: timeout: TimeoutError: slow"]


def test_two_frozen_runs_agree_per_item_without_routing(monkeypatch):
    """The determinism contract: same code + same dataset → same numbers.

    Every model/DB step is stubbed, so the only way the two runs can differ is
    the harness itself (routing, plan assembly, ordering). Routing is turned
    into a failure to prove the frozen path never calls it.
    """
    units = [{"evidence_id": "e1", "text": "a", "provenance": "text"}]
    _stub_chain(monkeypatch, units=units, citation_index={"e1": 0})
    monkeypatch.setattr(
        graph_module, "route", lambda *a, **k: pytest.fail("routed in frozen mode")
    )
    items = [_gold_item("q1"), _gold_item("q2", category="mechanism")]
    keys = ("plan_source", "queries", "query_hash", "routes", "rounds", "coverage")

    runs: list[list[dict]] = []
    for _ in range(2):
        runs.append(
            [
                eval_answers.run_item(item, arm, graph_module.run_query, frozen=True)
                for item in items
                for arm, _ in eval_answers.ARMS
            ]
        )

    assert [[r[k] for k in keys] for r in runs[0]] == [
        [r[k] for k in keys] for r in runs[1]
    ]
    by_item = {item["id"]: item for item in items}
    for record in runs[0]:
        assert record["plan_source"] == "frozen"
        assert record["queries"] == by_item[record["id"]]["expanded_queries"]
        assert record["query_hash"] == graph_module.query_hash(record["queries"])
        assert record["coverage"] == 1.0
        assert record["rounds"] == 1


def test_payload_records_plan_source_and_dataset_hash():
    payload = eval_answers.build_payload(
        dataset_path=Path("evals/gold/graph_expansion_v2.json"),
        digest="abc123",
        records=[_summary_record("a", "text", hard=True, coverage=1.0)],
        limit=0,
        timestamp="t",
        plan_source="frozen",
    )

    assert payload["plan_source"] == "frozen"
    assert payload["dataset_sha256"] == "abc123"


# ---------------------------------------------------------------------------
# checkpoint / resume
# ---------------------------------------------------------------------------


def _record(item_id: str, arm: str, status: str = "ok") -> dict:
    return {"id": item_id, "arm": arm, "status": status, "value": f"{item_id}-{arm}"}


def test_checkpoint_latest_record_wins_and_merge_is_stable(tmp_path):
    path = tmp_path / "checkpoint.jsonl"
    eval_answers.append_checkpoint(path, _record("q1", "text"))
    eval_answers.append_checkpoint(path, _record("q2", "text"))
    eval_answers.append_checkpoint(path, _record("q1", "graph"))
    eval_answers.append_checkpoint(path, _record("q1", "text", status="ok"))
    eval_answers.append_checkpoint(path, _record("q1", "text", status="error"))

    checkpoint = eval_answers.load_checkpoint(path)
    items = [{"id": "q2"}, {"id": "q1"}, {"id": "q3"}]
    merged = eval_answers.merge_records(items, eval_answers.ARMS, checkpoint)

    assert [(r["id"], r["arm"]) for r in merged] == [
        ("q2", "text"),
        ("q1", "text"),
        ("q1", "graph"),
    ]
    assert merged[1]["status"] == "error"  # last line wins


def test_pending_keys_skips_successes_and_retries_errors(tmp_path):
    path = tmp_path / "checkpoint.jsonl"
    eval_answers.append_checkpoint(path, _record("q1", "text"))
    eval_answers.append_checkpoint(path, _record("q1", "graph", status="error"))
    eval_answers.append_checkpoint(path, _record("q2", "text"))
    checkpoint = eval_answers.load_checkpoint(path)

    todo = eval_answers.pending_keys(
        [{"id": "q1"}, {"id": "q2"}, {"id": "q3"}], eval_answers.ARMS, checkpoint
    )

    assert [(item["id"], arm) for item, arm in todo] == [
        ("q1", "graph"),
        ("q2", "graph"),
        ("q3", "text"),
        ("q3", "graph"),
    ]


def test_checkpoint_ignores_a_partial_trailing_line(tmp_path):
    path = tmp_path / "checkpoint.jsonl"
    eval_answers.append_checkpoint(path, _record("q1", "text"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"id": "q2", "arm": "te')

    checkpoint = eval_answers.load_checkpoint(path)

    assert list(checkpoint) == [("q1", "text")]


# ---------------------------------------------------------------------------
# summaries
# ---------------------------------------------------------------------------


def _summary_record(
    item_id: str,
    arm: str,
    *,
    hard: bool,
    coverage: float,
    cited: int = 1,
    rounds: int = 1,
    status: str = "ok",
    failures: int = 0,
) -> dict:
    return {
        "id": item_id,
        "arm": arm,
        "status": status,
        "hard_pass": hard,
        "coverage": coverage,
        "coverage_complete": coverage == 1.0,
        "invalid_citations": [],
        "graph_citations": [],
        "cited_evidence_ids": ["e"] * cited,
        "citable_ids": ["e", "f"],
        "graph_evidence_ids": [],
        "rounds": rounds,
        "failures": ["f"] * failures,
        "elapsed_s": 1.0,
    }


def test_summary_arithmetic_per_arm():
    records = [
        _summary_record("a", "text", hard=True, coverage=1.0, cited=2, rounds=1),
        _summary_record("b", "text", hard=False, coverage=0.25, failures=1),
        _summary_record("c", "text", hard=False, coverage=0.0, status="error"),
    ]
    summary = eval_answers.summarize_arm(records)

    assert summary["items"] == 3
    assert summary["ok"] == 2
    assert summary["errors"] == 1
    assert summary["hard_pass"] == 1
    assert summary["hard_pass_rate"] == 0.5
    assert summary["coverage_mean"] == 0.625
    assert summary["coverage_distribution"] == {"0": 0, "(0,0.5)": 1, "[0.5,1)": 0, "1": 1}
    assert summary["coverage_complete_items"] == 1
    assert summary["citations_total"] == 3
    assert summary["retrieval_failures"] == 1


def test_delta_compares_graph_against_text_on_paired_items():
    records = [
        _summary_record("a", "text", hard=True, coverage=1.0),
        _summary_record("a", "graph", hard=True, coverage=1.0),
        _summary_record("b", "text", hard=True, coverage=0.5),
        _summary_record("b", "graph", hard=True, coverage=1.0),
        _summary_record("c", "text", hard=True, coverage=0.5),
        _summary_record("c", "graph", hard=False, coverage=0.0),
        _summary_record("d", "text", hard=False, coverage=0.0, status="error"),
        _summary_record("d", "graph", hard=True, coverage=0.5),
    ]
    delta = eval_answers.summarize_delta(records)

    assert delta["paired"] == 3  # d has one error and is excluded from pairing
    assert delta["text"]["hard_pass"] == 3
    assert delta["graph"]["hard_pass"] == 3  # arm-wide: a, b, d pass
    assert delta["hard_pass_delta"] == -1  # paired: c regresses
    assert delta["coverage_mean_delta"] == 0.0  # 0.6667 both sides
    assert delta["coverage_better_items"] == 1
    assert delta["coverage_worse_items"] == 1
    assert delta["coverage_equal_items"] == 1


def test_coverage_bucket_boundaries():
    assert eval_answers.coverage_bucket(0.0) == "0"
    assert eval_answers.coverage_bucket(0.49) == "(0,0.5)"
    assert eval_answers.coverage_bucket(0.5) == "[0.5,1)"
    assert eval_answers.coverage_bucket(1.0) == "1"
