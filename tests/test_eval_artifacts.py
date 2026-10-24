"""Deterministic gates over the frozen eval assets.

No index, no database, no API calls: everything here is bytes and arithmetic
over ``evals/gold/`` and the committed run records in ``.scratch/``. This is
the part of the evaluation that CI can enforce — it proves the datasets still
verify, the records still describe them, and chapter 14 still quotes those
records. It does not prove answer quality (that needs the production chain and
a model), which is why the docs call the numbers directional.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "evals" / "gold"
RUNS = ROOT / ".scratch" / "retrieval-eval-parity"
CHAPTER = ROOT / "book" / "chapter14-evaluation.md"
CHART_CHAPTER = ROOT / "book" / "chapter13-chart.md"
#: the only chart run the book may quote; a run against another dataset revision
#: is incomparable, which is what the ``dataset_sha256`` gate below enforces
CHART_RUN = ROOT / ".scratch" / "chart-analysis" / "eval" / "20261024T125057Z" / "summary.json"

DATASETS = ("graph_expansion.json", "graph_expansion_v2.json", "chart_v1.json")
ABLATIONS = (
    "gold-v2-text-w14.json",
    "gold-v2-text-w20.json",
    "gold-v2-graph-w14.json",
    "gold-v2-graph-w20.json",
)


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sidecar(name: str) -> str:
    return (GOLD / f"{name.removesuffix('.json')}.sha256").read_text().split()[0]


def test_datasets_match_their_sidecars():
    for name in DATASETS:
        digest = hashlib.sha256((GOLD / name).read_bytes()).hexdigest()
        assert digest == sidecar(name), name


def test_v2_dataset_keeps_frozen_strata_and_queries():
    items = load(GOLD / "graph_expansion_v2.json")["items"]

    assert len(items) == 60
    strata = {}
    for item in items:
        assert len(item["expanded_queries"]) == 6
        assert item["expected_evidence"]
        assert item["expected_points"]
        strata[item["category"]] = strata.get(item["category"], 0) + 1
    assert strata == {
        "encyclopedia": 20,
        "mechanism": 20,
        "exception": 10,
        "conditioned_claim": 10,
    }


@pytest.mark.parametrize("name", ABLATIONS)
def test_ablation_records_describe_the_frozen_dataset(name):
    record = load(RUNS / name)
    dataset = load(GOLD / "graph_expansion_v2.json")
    dataset_path = Path(record["dataset"].replace("\\", "/")).name

    assert dataset_path == "graph_expansion_v2.json"
    assert [item["id"] for item in record["items"]] == [
        item["id"] for item in dataset["items"]
    ]

    items = record["items"]
    expected_total = sum(len(item["expected"]) for item in items)
    if record["mode"] == "text":
        found = sum(len(item["found"]) for item in items)
        any_hit = sum(1 for item in items if item["found"])
        complete = sum(1 for item in items if not item["missing"])
    else:
        found = sum(len(item["graph_found"]) for item in items)
        complete = None

    summary = record["summary"]
    assert summary["expected_total"] == expected_total
    assert summary.get("expected_found", summary.get("graph_found")) == found
    if complete is not None:
        assert summary["items_with_any_hit"] == any_hit
        assert summary["items_with_all_hits"] == complete
    assert all(item["query_count"] == 6 for item in items)


def test_chapter14_quotes_the_committed_records():
    text = re.sub(r"\s+", " ", CHAPTER.read_text(encoding="utf-8"))

    for name in ABLATIONS:
        record = load(RUNS / name)
        summary = record["summary"]
        found = summary.get("expected_found", summary.get("graph_found"))
        if record["mode"] == "text":
            assert f"{found} / {summary['expected_total']}" in text, name
        else:
            assert f"{summary['graph_arm_found']} / {summary['expected_total']}" in text
            assert f"+{summary['net_gain']}" in text

    answers = load(max((RUNS / "answers").glob("*/summary.json"), key=_mtime))
    for arm in ("text", "graph"):
        assert f"{answers['summary'][arm]['coverage_mean']:.4f}" in text

    frozen = sidecar("graph_expansion_v2.json")
    matching = [
        path
        for path in (RUNS / "judgements").glob("*/summary.json")
        if load(path)["dataset_sha256"] == frozen
    ]
    judged = load(max(matching, key=_mtime))["by_arm"]
    for arm in ("text", "graph"):
        for metric in ("correctness", "condition_preservation", "citation_support"):
            assert f"{judged[arm]['scores'][metric]['mean']:.3f}" in text
    for arm in ("text", "graph"):
        assert str(judged[arm]["unsupported_claims"]["total"]) in text


def test_chart_run_uses_the_frozen_dataset():
    # a run against another revision is incomparable — that is why the book may
    # not quote `.scratch/chart-analysis/eval/dev` (hash `18d205d3…`)
    assert load(CHART_RUN)["dataset_sha256"] == sidecar("chart_v1.json")


def test_docs_quote_the_chart_run():
    summary = load(CHART_RUN)["summary"]
    rates = [
        f"{summary['miss_rate'] * 100:.1f}%",
        f"{summary['extra_rate'] * 100:.1f}%",
    ]
    total = str(summary["expected_levels_total"])
    # chapter 13 quotes the bucket row; chapter 14 quotes the ratios; chapter 12
    # only carries the rates; the overview pages add the denominator
    per_chapter = {
        CHART_CHAPTER: [
            total,
            *rates,
            f"{summary['matched_total']} / {summary['missed_total']} / "
            f"{summary['extra_total']} / {summary['drifted_total']}",
        ],
        CHAPTER: [
            total,
            *rates,
            f"{summary['missed_total']}/{summary['expected_levels_total']}",
            f"{summary['extra_total']}/{summary['answered_levels_total']}",
        ],
        ROOT / "README.md": [total, *rates],
        ROOT / "book" / "chapter11-ai-review.md": [total, *rates],
        ROOT / "book" / "chapter12-allinrag-comparison.md": rates,
    }

    for chapter, fragments in per_chapter.items():
        text = re.sub(r"\s+", " ", chapter.read_text(encoding="utf-8"))
        for fragment in fragments:
            assert fragment in text, (chapter.name, fragment)


def _mtime(path: Path) -> float:
    return path.stat().st_mtime
