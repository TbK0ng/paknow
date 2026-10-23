"""Answer-level runner: arm passthrough, checkpoint merge, hard constraints and
summary arithmetic. No real model and no Qdrant: every chain step is a stub.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from paknow.retrieval.pipeline import RetrievalResult, citation_binding
from paknow.workflows import graph as graph_module
from paknow.workflows.router import QueryPlan
from scripts import eval_answers


# ---------------------------------------------------------------------------
# run_query passthrough
# ---------------------------------------------------------------------------


def _stub_chain(monkeypatch, *, units, citation_index):
    """Replace every model/DB step of the production chain with a stub."""
    calls: list[dict] = []

    def fake_retrieve(query, queries, **kwargs):
        calls.append({"query": query, "queries": list(queries), **kwargs})
        return RetrievalResult(
            text_hits=[u for u in units if u.get("provenance") != "derived_observation"],
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
        lambda *a, **k: SimpleNamespace(
            next_query=None,
            model_dump=lambda: {"sufficient": True, "next_query": None},
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

    def fake_run_query(query, *, expand_graph):
        seen.append((query, expand_graph))
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

    assert seen == [("问法", False)]
    assert record["status"] == "ok"
    assert record["hard_pass"] is True
    assert record["coverage"] == 1.0
    assert record["rounds"] == 2


def test_run_item_records_a_failure_and_keeps_the_batch_alive():
    def boom(query, *, expand_graph):
        raise RuntimeError("gateway down")

    item = {"id": "q1", "category": "mechanism", "question": "q", "expected_evidence": ["e1"]}
    record = eval_answers.run_item(item, "graph", boom)

    assert record["status"] == "error"
    assert record["arm"] == "graph"
    assert "gateway down" in record["error"]
    assert record["hard_pass"] is False
    assert record["expected_missing"] == ["e1"]


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
