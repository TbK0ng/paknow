"""Gold-standard evaluation: frozen queries, per-question graph diff, the fixed
graph-unique verdict, truncation reporting, and a deterministic report shape.

These tests cover the pure evaluation logic only; the evaluation itself is an
offline run against the text index (not a CI gate).
"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

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
# graph-only metric and net gain
# ---------------------------------------------------------------------------


def test_graph_only_is_expected_graph_evidence_the_text_arm_lacks():
    assert eval_gold.graph_only(
        ["a", "b", "c"], text_ids={"a"}, graph_ids={"a", "b", "z"}
    ) == ["b"]
    assert eval_gold.graph_only(["a"], text_ids={"a"}, graph_ids={"a", "b"}) == []


def test_union_found_is_expected_units_of_either_arm():
    assert eval_gold.union_found(
        ["a", "b", "c"], text_ids={"a", "b"}, graph_ids={"b", "c"}
    ) == ["a", "b", "c"]
    assert eval_gold.union_found(["a", "d"], text_ids={"a"}, graph_ids={"c"}) == ["a"]


def test_net_gain_counts_the_graph_arm_union_not_graph_evidence():
    # text {a,b} + graph {b,c}: the overlap b must not cancel the gain
    rows = [
        {**_row("mech-01"), "graph_found": ["b", "c"],
         "regression_text_found": ["a", "b"], "graph_only": ["c"],
         "union_found": ["a", "b", "c"]},
    ]

    summary = eval_gold.summarize_graph(rows)

    assert summary["graph_found"] == 2
    assert summary["graph_only"] == 1
    assert summary["regression_text_found"] == 2
    assert summary["union_found"] == 3
    assert summary["net_gain"] == 1


def test_net_gain_totals_union_found_across_rows():
    rows = [
        {**_row("mech-01"), "graph_found": ["a", "b"], "regression_text_found": ["a"],
         "graph_only": ["b"], "union_found": ["a", "b"]},
        {**_row("mech-02"), "graph_found": [], "regression_text_found": ["a"],
         "graph_only": [], "union_found": ["a"]},
    ]

    summary = eval_gold.summarize_graph(rows)

    assert summary["union_found"] == 3
    assert summary["regression_text_found"] == 2
    assert summary["net_gain"] == 1


def test_net_gain_is_zero_when_both_arms_reach_the_same_units():
    rows = [
        {**_row("mech-01"), "graph_found": [], "regression_text_found": ["a", "b"],
         "graph_only": [], "union_found": ["a", "b"]},
    ]

    summary = eval_gold.summarize_graph(rows)

    assert summary["net_gain"] == 0


def test_evaluate_text_item_runs_the_seam_with_graph_off(monkeypatch):
    seen = {}

    def fake_retrieve(query, queries, **kwargs):
        seen["query"] = query
        seen["queries"] = list(queries)
        seen["kwargs"] = kwargs
        return _FakeResult(["a"], [], {"enabled": False, "selected": 0})

    monkeypatch.setattr(eval_gold, "retrieve_evidence", fake_retrieve)
    item = {
        "id": "enc-01",
        "category": "encyclopedia",
        "question": "q",
        "expanded_queries": ["q", "q2"],
        "expected_evidence": ["a", "b"],
    }

    row = eval_gold.evaluate_text_item(item)

    assert seen == {
        "query": "q",
        "queries": ["q", "q2"],
        "kwargs": {"expand_graph": False, "text_width": 14},
    }
    assert row["found"] == ["a"]
    assert row["missing"] == ["b"]
    assert row["hit"] is True
    assert row["hits"] == ["a"]
    assert row["graph_stats"]["enabled"] is False
    assert row["failures"] == []


def test_evaluate_text_item_passes_the_requested_width(monkeypatch):
    seen = {}

    def fake_retrieve(query, queries, **kwargs):
        seen.update(kwargs)
        return _FakeResult(["a"], [], {"enabled": False, "selected": 0})

    monkeypatch.setattr(eval_gold, "retrieve_evidence", fake_retrieve)
    item = {
        "id": "enc-01",
        "category": "encyclopedia",
        "question": "q",
        "expanded_queries": ["q"],
        "expected_evidence": ["a"],
    }

    eval_gold.evaluate_text_item(item, text_width=20)

    assert seen == {"expand_graph": False, "text_width": 20}


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
        "graph_only": ["a"],
        "union_found": ["a"],
        "graph_entries": 1,
        "graph_stats": {"selected": 1, "tokens": 12},
        "truncation": {"truncated": False, "hit_limits": [], "selected": 1, "tokens": 12},
        "failures": [],
        "query_count": 6,
    }


def test_regression_payload_has_a_stable_key_order_and_is_serializable():
    dataset = {"frozen_at": "2026-10-23", "retrieval_protocol": "p"}
    baseline = {"summary": {"expected_found": 24}, "items": []}

    text_summary = {
        "items": 1,
        "expected_total": 1,
        "expected_found": 0,
        "expected_missing": 1,
        "items_with_any_hit": 0,
        "items_with_all_hits": 0,
    }
    payload = eval_gold.build_regression_payload(
        dataset_path="evals/gold/graph_expansion.json",
        dataset=dataset,
        baseline=baseline,
        rows=[_row("mech-01")],
        text_summary=text_summary,
        top_k=20,
        text_width=20,
    )

    assert list(payload) == [
        "dataset",
        "frozen_at",
        "protocol",
        "top_k",
        "text_width",
        "mode",
        "baseline_reused",
        "baseline_summary",
        "text_arm_summary",
        "summary",
        "items",
    ]
    assert payload["mode"] == "graph"
    assert payload["text_width"] == 20
    assert payload["baseline_reused"] is False
    assert payload["text_arm_summary"] == text_summary
    assert payload["items"][0]["id"] == "mech-01"
    first = json.dumps(payload, ensure_ascii=False, indent=2)
    again = json.dumps(
        eval_gold.build_regression_payload(
            dataset_path="evals/gold/graph_expansion.json",
            dataset=dataset,
            baseline=baseline,
            rows=[_row("mech-01")],
            text_summary=text_summary,
            top_k=20,
            text_width=20,
        ),
        ensure_ascii=False,
        indent=2,
    )
    assert first == again


def test_text_payload_identifies_the_production_parity_mode():
    dataset = {"frozen_at": "2026-10-23", "retrieval_protocol": "p"}
    rows = [
        {
            "id": "enc-01",
            "category": "encyclopedia",
            "question": "q",
            "expected": ["a", "b"],
            "found": ["a"],
            "missing": ["b"],
            "hit": True,
            "hits": ["a"],
        }
    ]

    payload = eval_gold.build_text_payload(
        dataset_path="evals/gold/graph_expansion.json",
        dataset=dataset,
        rows=rows,
        top_k=20,
        text_width=20,
    )

    assert list(payload) == [
        "dataset",
        "frozen_at",
        "protocol",
        "top_k",
        "text_width",
        "mode",
        "expand_graph",
        "summary",
        "items",
    ]
    assert payload["mode"] == "text"
    assert payload["expand_graph"] is False
    assert payload["text_width"] == 20
    assert payload["summary"]["expected_found"] == 1


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
    assert row["graph_only"] == ["b"]
    assert row["lost"] == []
    assert row["still_missing"] == ["c"]
    assert row["graph_unique"] is True
    assert row["truncation"]["truncated"] is False


def test_evaluate_graph_item_passes_the_requested_width(monkeypatch):
    seen = {}

    def fake_retrieve(query, queries, **kwargs):
        seen.update(kwargs)
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
        "expanded_queries": ["q"],
        "expected_evidence": ["a", "b"],
    }

    eval_gold.evaluate_graph_item(item, baseline_union={"a"}, text_width=20)

    assert seen == {"text_width": 20}


# ---------------------------------------------------------------------------
# frozen dataset hash for baseline reuse
# ---------------------------------------------------------------------------


def _write(path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def test_committed_sidecar_matches_the_frozen_dataset():
    sidecar = eval_gold.DEFAULT_DATASET_HASH
    assert sidecar.exists()
    expected = eval_gold.read_dataset_hash(sidecar)
    actual = eval_gold.dataset_sha256(eval_gold.DEFAULT_DATASET)

    assert expected == actual
    assert len(actual) == 64


def test_verify_frozen_dataset_accepts_a_matching_sidecar(tmp_path):
    dataset = tmp_path / "dataset.json"
    _write(dataset, '{"items": []}')
    digest = hashlib.sha256(dataset.read_bytes()).hexdigest()
    sidecar = tmp_path / "dataset.sha256"
    _write(sidecar, digest + "\n")

    assert eval_gold.verify_frozen_dataset(dataset, sidecar=sidecar) == digest


def test_verify_frozen_dataset_accepts_sha256sum_output(tmp_path):
    dataset = tmp_path / "dataset.json"
    _write(dataset, '{"items": []}')
    digest = hashlib.sha256(dataset.read_bytes()).hexdigest()
    sidecar = tmp_path / "dataset.sha256"
    _write(sidecar, f"{digest}  dataset.json\n")

    assert eval_gold.verify_frozen_dataset(dataset, sidecar=sidecar) == digest


def test_verify_frozen_dataset_rejects_a_changed_dataset(tmp_path):
    dataset = tmp_path / "dataset.json"
    _write(dataset, '{"items": []}')
    sidecar = tmp_path / "dataset.sha256"
    _write(sidecar, hashlib.sha256(b"something else").hexdigest())

    with pytest.raises(SystemExit) as excinfo:
        eval_gold.verify_frozen_dataset(dataset, sidecar=sidecar)

    message = str(excinfo.value)
    assert "hash mismatch" in message
    assert "rerun --mode baseline" in message


def test_verify_frozen_dataset_requires_the_sidecar(tmp_path):
    dataset = tmp_path / "dataset.json"
    _write(dataset, '{"items": []}')
    sidecar = tmp_path / "missing.sha256"

    with pytest.raises(SystemExit) as excinfo:
        eval_gold.verify_frozen_dataset(dataset, sidecar=sidecar)

    message = str(excinfo.value)
    assert "sidecar" in message
    assert str(sidecar) in message


def test_verify_frozen_dataset_rejects_a_malformed_sidecar(tmp_path):
    dataset = tmp_path / "dataset.json"
    _write(dataset, '{"items": []}')
    sidecar = tmp_path / "dataset.sha256"
    _write(sidecar, "not-a-sha256\n")

    with pytest.raises(SystemExit, match="SHA-256"):
        eval_gold.verify_frozen_dataset(dataset, sidecar=sidecar)


def test_reuse_baseline_is_rejected_outside_graph_mode():
    with pytest.raises(SystemExit, match="only valid with --mode graph"):
        eval_gold.require_reuse_mode("baseline", reuse=True)
    eval_gold.require_reuse_mode("graph", reuse=True)
    eval_gold.require_reuse_mode("baseline", reuse=False)


def test_reuse_baseline_flag_fails_fast_in_baseline_mode(monkeypatch):
    monkeypatch.setattr(
        "sys.argv", ["eval_gold.py", "--mode", "baseline", "--reuse-baseline"]
    )

    with pytest.raises(SystemExit, match="only valid with --mode graph"):
        eval_gold.main()


def _reuse_fixture(tmp_path):
    dataset = tmp_path / "dataset.json"
    dataset.write_text(
        json.dumps(
            {
                "version": 1,
                "frozen_at": "2026-10-23",
                "retrieval_protocol": "p",
                "queries_per_item": 6,
                "items": [
                    {
                        "id": "enc-01",
                        "category": "encyclopedia",
                        "question": "q",
                        "expanded_queries": ["q", "q2", "q3", "q4", "q5", "q6"],
                        "expected_evidence": ["a", "b"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    sidecar = tmp_path / "dataset.sha256"
    sidecar.write_text(
        hashlib.sha256(dataset.read_bytes()).hexdigest() + "\n", encoding="utf-8"
    )
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "dataset": str(dataset),
                "frozen_at": "2026-10-23",
                "protocol": "p",
                "top_k": 20,
                "summary": {"expected_found": 1},
                "items": [
                    {
                        "id": "enc-01",
                        "category": "encyclopedia",
                        "question": "q",
                        "expected": ["a", "b"],
                        "found": ["a"],
                        "missing": ["b"],
                        "hit": True,
                        "graph_unique": True,
                        "union_size": 1,
                        "hits": ["a"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return dataset, sidecar, baseline


def test_reuse_baseline_writes_verified_provenance_without_touching_baseline(
    tmp_path, monkeypatch
):
    dataset, sidecar, baseline = _reuse_fixture(tmp_path)
    before = baseline.read_bytes()
    out = tmp_path / "regression.json"

    def fake_retrieve(query, queries, **kwargs):
        return _FakeResult(
            ["a"],
            ["b"],
            {"selected": 1, "tokens": 10, "truncated_edges": False,
             "truncated_evidence": False, "truncated_tokens": False},
        )

    monkeypatch.setattr(eval_gold, "retrieve_evidence", fake_retrieve)
    monkeypatch.setattr(
        "sys.argv",
        [
            "eval_gold.py",
            "--mode", "graph",
            "--dataset", str(dataset),
            "--dataset-hash", str(sidecar),
            "--baseline", str(baseline),
            "--out", str(out),
            "--reuse-baseline",
        ],
    )

    eval_gold.main()

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["baseline_reused"] is True
    assert payload["text_width"] == 14
    assert payload["summary"]["newly_recalled"] == 1
    assert payload["summary"]["graph_only"] == 1
    assert payload["summary"]["union_found"] == 2  # text found a, graph found b
    assert payload["summary"]["net_gain"] == 1
    assert payload["text_arm_summary"]["expected_found"] == 1
    assert payload["dataset"] == str(dataset)
    assert baseline.read_bytes() == before


def test_text_mode_writes_its_own_artifact_with_graph_off(tmp_path, monkeypatch):
    dataset, _sidecar, baseline = _reuse_fixture(tmp_path)
    before = baseline.read_bytes()
    out = tmp_path / "text.json"
    calls = []

    def fake_retrieve(query, queries, **kwargs):
        calls.append(dict(kwargs))
        return _FakeResult(
            ["a"],
            [],
            {"enabled": False, "selected": 0, "accumulated": 0, "candidates": 0,
             "truncated_edges": False, "truncated_evidence": False,
             "truncated_tokens": False, "tokens": 0},
        )

    monkeypatch.setattr(eval_gold, "retrieve_evidence", fake_retrieve)
    monkeypatch.setattr(
        "sys.argv",
        [
            "eval_gold.py",
            "--mode", "text",
            "--dataset", str(dataset),
            "--baseline", str(baseline),
            "--out", str(out),
        ],
    )

    eval_gold.main()

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert calls == [{"expand_graph": False, "text_width": 14}]
    assert payload["mode"] == "text"
    assert payload["expand_graph"] is False
    assert payload["text_width"] == 14
    assert payload["summary"]["expected_found"] == 1
    assert payload["items"][0]["graph_stats"]["enabled"] is False
    assert payload["items"][0]["failures"] == []
    assert baseline.read_bytes() == before


def test_text_mode_records_the_requested_width(tmp_path, monkeypatch):
    dataset, _sidecar, baseline = _reuse_fixture(tmp_path)
    out = tmp_path / "text.json"
    calls = []

    def fake_retrieve(query, queries, **kwargs):
        calls.append(dict(kwargs))
        return _FakeResult(
            ["a"],
            [],
            {"enabled": False, "selected": 0, "accumulated": 0, "candidates": 0,
             "truncated_edges": False, "truncated_evidence": False,
             "truncated_tokens": False, "tokens": 0},
        )

    monkeypatch.setattr(eval_gold, "retrieve_evidence", fake_retrieve)
    monkeypatch.setattr(
        "sys.argv",
        [
            "eval_gold.py",
            "--mode", "text",
            "--dataset", str(dataset),
            "--baseline", str(baseline),
            "--text-width", "20",
            "--out", str(out),
        ],
    )

    eval_gold.main()

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert calls == [{"expand_graph": False, "text_width": 20}]
    assert payload["text_width"] == 20


def test_graph_mode_applies_the_width_to_both_arms(tmp_path, monkeypatch):
    dataset, sidecar, baseline = _reuse_fixture(tmp_path)
    out = tmp_path / "regression.json"
    calls = []

    def fake_retrieve(query, queries, **kwargs):
        calls.append(dict(kwargs))
        return _FakeResult(
            ["a"],
            ["b"],
            {"selected": 1, "tokens": 10, "truncated_edges": False,
             "truncated_evidence": False, "truncated_tokens": False},
        )

    monkeypatch.setattr(eval_gold, "retrieve_evidence", fake_retrieve)
    monkeypatch.setattr(
        "sys.argv",
        [
            "eval_gold.py",
            "--mode", "graph",
            "--dataset", str(dataset),
            "--dataset-hash", str(sidecar),
            "--baseline", str(baseline),
            "--text-width", "20",
            "--out", str(out),
            "--reuse-baseline",
        ],
    )

    eval_gold.main()

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert [call["text_width"] for call in calls] == [20, 20]
    assert calls[0]["expand_graph"] is False  # production-parity text arm
    assert "expand_graph" not in calls[1]  # shipped graph arm
    assert payload["text_width"] == 20


def test_invalid_text_width_is_rejected(monkeypatch):
    monkeypatch.setattr(
        "sys.argv", ["eval_gold.py", "--mode", "text", "--text-width", "0"]
    )

    with pytest.raises(SystemExit):
        eval_gold.main()


def test_baseline_payload_records_the_width(tmp_path, monkeypatch):
    dataset, _sidecar, _baseline = _reuse_fixture(tmp_path)
    out = tmp_path / "baseline.json"

    def fake_search(query, *, top_k=20):
        return [SimpleNamespace(payload={"evidence_id": "a"})]

    monkeypatch.setattr(eval_gold, "hybrid_search", fake_search)
    monkeypatch.setattr(
        "sys.argv",
        [
            "eval_gold.py",
            "--mode", "baseline",
            "--dataset", str(dataset),
            "--text-width", "20",
            "--out", str(out),
        ],
    )

    eval_gold.main()

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["text_width"] == 20
    assert payload["summary"]["expected_found"] == 1

