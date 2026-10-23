"""Gold-standard evaluation: frozen queries, per-question graph diff, the fixed
graph-unique verdict, truncation reporting, and a deterministic report shape.

These tests cover the pure evaluation logic only; the evaluation itself is an
offline run against the text index (not a CI gate).
"""
from __future__ import annotations

import json

from scripts import eval_gold


# ---------------------------------------------------------------------------
# frozen dataset
# ---------------------------------------------------------------------------


def test_dataset_keeps_frozen_query_count_and_shape():
    data = eval_gold.load_dataset(eval_gold.DEFAULT_DATASET)

    assert data["queries_per_item"] == 6
    assert len(data["items"]) == 20
    for item in data["items"]:
        assert len(item["expanded_queries"]) == 6
        assert item["expected_evidence"]
        assert item["category"] in {"encyclopedia", "mechanism"}


# ---------------------------------------------------------------------------
# fixed graph-unique verdict
# ---------------------------------------------------------------------------


def test_graph_unique_is_any_expected_outside_baseline_union():
    assert eval_gold.graph_unique(["a", "b"], {"a"}) is True
    assert eval_gold.graph_unique(["a"], {"a", "z"}) is False
    assert eval_gold.graph_unique([], set()) is False


# ---------------------------------------------------------------------------
# per-question diff
# ---------------------------------------------------------------------------


def test_classify_diff_separates_newly_recalled_lost_and_still_missing():
    out = eval_gold.classify_diff(
        ["a", "b", "c", "d"],
        baseline_union={"a", "b"},
        text_ids={"a", "b"},
        graph_ids={"c"},
    )

    assert out["baseline_found"] == ["a", "b"]
    assert out["baseline_missing"] == ["c", "d"]
    assert out["recovered_expected"] == ["c"]
    assert out["newly_recalled"] == ["c"]
    assert out["lost"] == []
    assert out["text_path_lost"] == []
    assert out["still_missing"] == ["d"]
    assert out["graph_unique"] is True
    assert out["graph_unique_recovered"] is True


def test_baseline_evidence_the_new_text_path_drops_is_reported_as_lost():
    out = eval_gold.classify_diff(
        ["a", "b"],
        baseline_union={"a", "b"},
        text_ids={"a"},
        graph_ids=set(),
    )

    assert out["lost"] == ["b"]
    assert out["text_path_lost"] == ["b"]
    assert out["newly_recalled"] == []


def test_graph_evidence_already_in_baseline_is_not_newly_recalled():
    out = eval_gold.classify_diff(
        ["a", "b"],
        baseline_union={"a"},
        text_ids={"a"},
        graph_ids={"a", "b"},
    )

    assert out["newly_recalled"] == ["b"]
    assert out["graph_unique_recovered"] is True


def test_expected_unit_found_by_the_new_text_path_is_not_graph_attributable():
    out = eval_gold.classify_diff(
        ["x"],
        baseline_union=set(),
        text_ids={"x"},
        graph_ids={"x"},
    )

    assert out["recovered_expected"] == ["x"]
    assert out["newly_recalled"] == []
    assert out["graph_unique_recovered"] is True  # the verdict does not depend on attribution


# ---------------------------------------------------------------------------
# truncation
# ---------------------------------------------------------------------------


def test_truncation_report_names_the_budget_that_bit():
    stats = {
        "candidates": 9,
        "selected": 8,
        "truncated_edges": False,
        "truncated_evidence": True,
        "truncated_tokens": False,
        "tokens": 400,
    }

    report = eval_gold.truncation_report(stats, entries=[{}] * 8)

    assert report["truncated"] is True
    assert report["hit_limits"] == ["evidence"]
    assert report["selected"] == 8
    assert report["tokens"] == 400


def test_no_truncation_report_is_empty_but_explicit():
    report = eval_gold.truncation_report(
        {"selected": 2, "tokens": 90, "truncated_edges": False,
         "truncated_evidence": False, "truncated_tokens": False},
        entries=[{}, {}],
    )

    assert report == {
        "truncated": False,
        "hit_limits": [],
        "selected": 2,
        "tokens": 90,
    }


# ---------------------------------------------------------------------------
# deterministic report shape
# ---------------------------------------------------------------------------


def _row(row_id: str) -> dict:
    return {
        "id": row_id,
        "category": "mechanism",
        "question": "q",
        "expected": ["a"],
        "baseline_found": [],
        "baseline_missing": ["a"],
        "regression_text_found": [],
        "graph_found": ["a"],
        "recovered_expected": ["a"],
        "newly_recalled": ["a"],
        "lost": [],
        "text_path_lost": [],
        "still_missing": [],
        "graph_unique": True,
        "graph_unique_recovered": True,
        "graph_entries": 1,
        "graph_stats": {"selected": 1, "tokens": 12},
        "truncation": {"truncated": False, "hit_limits": [], "selected": 1, "tokens": 12},
        "failures": [],
        "query_count": 6,
    }


def test_regression_payload_has_a_stable_key_order_and_is_serializable():
    dataset = {"frozen_at": "2026-10-23", "retrieval_protocol": "p"}
    baseline = {"summary": {"expected_found": 24}, "items": []}

    payload = eval_gold.build_regression_payload(
        dataset_path="evals/gold/graph_expansion.json",
        dataset=dataset,
        baseline=baseline,
        rows=[_row("mech-01")],
        top_k=20,
    )

    assert list(payload) == [
        "dataset",
        "frozen_at",
        "protocol",
        "top_k",
        "mode",
        "baseline_summary",
        "summary",
        "items",
    ]
    assert payload["mode"] == "graph"
    assert payload["items"][0]["id"] == "mech-01"
    first = json.dumps(payload, ensure_ascii=False, indent=2)
    again = json.dumps(
        eval_gold.build_regression_payload(
            dataset_path="evals/gold/graph_expansion.json",
            dataset=dataset,
            baseline=baseline,
            rows=[_row("mech-01")],
            top_k=20,
        ),
        ensure_ascii=False,
        indent=2,
    )
    assert first == again


# ---------------------------------------------------------------------------
# one question through the retrieval seam
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, text_ids, graph_ids, stats):
        self.text_hits = [{"evidence_id": e} for e in text_ids]
        self.graph_evidence = [{"evidence_id": e} for e in graph_ids]
        self.graph_stats = stats
        self.failures = []
        self.query_count = 6


def test_evaluate_graph_item_reuses_the_frozen_queries(monkeypatch):
    seen = {}

    def fake_retrieve(query, queries, **kwargs):
        seen["query"] = query
        seen["queries"] = list(queries)
        return _FakeResult(
            ["a"],
            ["b"],
            {"selected": 1, "tokens": 10, "truncated_edges": False,
             "truncated_evidence": False, "truncated_tokens": False},
        )

    monkeypatch.setattr(eval_gold, "retrieve_evidence", fake_retrieve)
    item = {
        "id": "enc-01",
        "category": "encyclopedia",
        "question": "q",
        "expanded_queries": ["q", "q2", "q3", "q4", "q5", "q6"],
        "expected_evidence": ["a", "b", "c"],
    }

    row = eval_gold.evaluate_graph_item(item, baseline_union={"a"})

    assert seen == {"query": "q", "queries": item["expanded_queries"]}
    assert seen["queries"] is not item["expanded_queries"]  # no mutation of the frozen list
    assert row["newly_recalled"] == ["b"]
    assert row["lost"] == []
    assert row["still_missing"] == ["c"]
    assert row["graph_unique"] is True
    assert row["truncation"]["truncated"] is False
