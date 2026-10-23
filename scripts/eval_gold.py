"""Offline gold-standard evaluation for graph expansion.

Runs the 20 frozen gold questions against retrieval and reports, per question,
whether each expected evidence unit appears in the union of the six frozen
queries' top-K results. Queries are read from the dataset and never regenerated,
so baseline and regression runs are comparable.

Three modes:

- ``baseline`` (union probe): per frozen query ``hybrid_search(top_k=20)`` and
  the union of their hits. This is the recorded upper-bound reference and its
  artifact must stay byte-identical across runs.
- ``text`` (production-parity baseline): one ``retrieve_evidence`` round per
  question over the same frozen queries with ``expand_graph=False`` — the same
  seam, rerank and diversity the online text channel uses, only the graph
  layer off. This is the arm the graph result is subtracted from.
- ``graph`` (shipped chain): one ``retrieve_evidence`` round with graph
  expansion on, plus one production-parity text round per question, then a
  per-question diff against both arms: expected units newly recalled over the
  union probe, graph-only expected units (in graph evidence but not in the
  text arm), baseline units lost by the new text path, budget truncations,
  the fixed graph-unique verdict and the net gain. The graph arm's expected
  hits are text hits plus graph evidence (``union_found``), so net gain is
  ``union_found - regression_text_found``. Truncation flags stay in the report
  but are not a verdict.

Graph mode can reuse an already recorded union-probe baseline with
``--reuse-baseline``. That path verifies ``evals/gold/graph_expansion.sha256``
against the dataset bytes before trusting the artifact, records
``baseline_reused: true`` in the report, and never rewrites the baseline JSON.
A missing sidecar or a hash mismatch is a hard failure: the frozen dataset
changed, so rerun the baseline. ``--mode text`` writes its own artifact path
and never touches the union-probe file.

``--text-width`` (default 14) is the recall width handed to the diversity step.
``text`` and ``graph`` apply it to every arm they run, so the width ablation can
vary one parameter while everything else stays fixed. The union probe has no
diversity step: the flag is accepted and recorded there, but its numbers do not
depend on it. The production default is unchanged, so a plain run reproduces the
online width.

This is an offline evaluation, not a CI gate: it needs the text index and the
embedding model. It does not call the routing model.

Usage:
    python scripts/eval_gold.py
    python scripts/eval_gold.py --mode text
    python scripts/eval_gold.py --mode graph
    python scripts/eval_gold.py --mode graph --text-width 20
    python scripts/eval_gold.py --mode graph --reuse-baseline
    python scripts/eval_gold.py --limit 4 --out .scratch/graph-expansion/baseline.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paknow.retrieval.pipeline import retrieve_evidence  # noqa: E402
from paknow.retrieval.search import hybrid_search  # noqa: E402

DEFAULT_DATASET = ROOT / "evals" / "gold" / "graph_expansion.json"
DEFAULT_DATASET_HASH = ROOT / "evals" / "gold" / "graph_expansion.sha256"
DEFAULT_BASELINE = ROOT / ".scratch" / "graph-expansion" / "gold-baseline.json"
DEFAULT_BASELINE_OUT = ROOT / ".scratch" / "graph-expansion" / "gold-baseline.json"
DEFAULT_TEXT_OUT = ROOT / ".scratch" / "retrieval-eval-parity" / "gold-text.json"
DEFAULT_GRAPH_OUT = ROOT / ".scratch" / "retrieval-eval-parity" / "gold-regression.json"

_HASH_HEX = frozenset("0123456789abcdef")

_TRUNCATION_REASONS = (
    ("edges", "truncated_edges"),
    ("evidence", "truncated_evidence"),
    ("tokens", "truncated_tokens"),
)


def load_dataset(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def dataset_sha256(path: Path) -> str:
    """SHA-256 of the dataset file bytes, so sidecars match independent tools."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_dataset_hash(sidecar: Path) -> str:
    """First token of the sidecar, accepting plain hex or ``sha256sum`` output."""
    raw = sidecar.read_text(encoding="utf-8")
    tokens = raw.split()
    digest = tokens[0].lower() if tokens else ""
    if len(digest) != 64 or not set(digest) <= _HASH_HEX:
        raise SystemExit(
            f"invalid dataset hash sidecar {sidecar}: expected a 64-character "
            f"SHA-256 hex digest, got {raw.strip()!r}"
        )
    return digest


def verify_frozen_dataset(
    dataset: Path, *, sidecar: Path = DEFAULT_DATASET_HASH
) -> str:
    """Fail unless the dataset bytes still match the recorded SHA-256."""
    if not sidecar.exists():
        raise SystemExit(
            f"no dataset hash sidecar at {sidecar}; --reuse-baseline needs one to "
            f"prove {dataset} is frozen. Generate it with:\n"
            f"  sha256sum {dataset} > {sidecar}"
        )
    expected = read_dataset_hash(sidecar)
    actual = dataset_sha256(dataset)
    if actual != expected:
        raise SystemExit(
            f"frozen dataset hash mismatch for {dataset}:\n"
            f"  sidecar {sidecar}: {expected}\n"
            f"  actual: {actual}\n"
            "the gold dataset changed; rerun --mode baseline and update the sidecar"
        )
    return actual


def require_reuse_mode(mode: str, reuse: bool) -> None:
    """Reusing a baseline only makes sense for the graph diff."""
    if reuse and mode != "graph":
        raise SystemExit("--reuse-baseline is only valid with --mode graph")


def _evidence_ids(units: Iterable[Mapping[str, Any]]) -> list[str]:
    """Evidence ids in first-seen order, deduped; units without ids are skipped."""
    out: dict[str, None] = {}
    for unit in units:
        eid = unit.get("evidence_id")
        if eid:
            out.setdefault(str(eid), None)
    return list(out)


def union_for_queries(queries: list[str], *, top_k: int) -> list[str]:
    """Top-K union across frozen queries, preserving first-seen order."""
    seen: dict[str, None] = {}
    for q in queries:
        for point in hybrid_search(q, top_k=top_k):
            eid = (point.payload or {}).get("evidence_id")
            if eid:
                seen.setdefault(eid, None)
    return list(seen)


# ---------------------------------------------------------------------------
# graph-mode classification (pure)
# ---------------------------------------------------------------------------


def graph_unique(expected: Sequence[str], baseline_union: Iterable[str]) -> bool:
    """Fixed metric: at least one expected unit outside the baseline union."""
    base = set(baseline_union)
    return any(eid not in base for eid in expected)


def graph_only(
    expected: Sequence[str],
    *,
    text_ids: Iterable[str],
    graph_ids: Iterable[str],
) -> list[str]:
    """Primary recall metric: expected units graph evidence adds over the text arm.

    It is production-parity: ``text_ids`` come from the same seam with graph
    expansion off, so a unit both arms reach cancels instead of scoring.
    """
    text = set(text_ids)
    graph = set(graph_ids)
    return [eid for eid in expected if eid in graph and eid not in text]


def union_found(
    expected: Sequence[str],
    *,
    text_ids: Iterable[str],
    graph_ids: Iterable[str],
) -> list[str]:
    """Expected units the graph arm reaches: text hits plus graph evidence."""
    covered = set(text_ids) | set(graph_ids)
    return [eid for eid in expected if eid in covered]


def classify_diff(
    expected: Sequence[str],
    *,
    baseline_union: Iterable[str],
    text_ids: Iterable[str],
    graph_ids: Iterable[str],
) -> dict[str, Any]:
    """Per-question diff between the baseline union and the shipped chain.

    ``newly_recalled`` is graph-attributable: the unit is in graph evidence,
    was not in the baseline union, and was not already reached by the new text
    path. ``recovered_expected`` is the broader "the graph surfaced a unit the
    baseline missed" set, whether or not the text path also found it.
    """
    base = set(baseline_union)
    text = set(text_ids)
    graph = set(graph_ids)

    baseline_found = [eid for eid in expected if eid in base]
    baseline_missing = [eid for eid in expected if eid not in base]
    graph_found = [eid for eid in expected if eid in graph]
    recovered = [eid for eid in baseline_missing if eid in graph]
    newly = [eid for eid in recovered if eid not in text]
    text_path_lost = [eid for eid in baseline_found if eid not in text]
    lost = [eid for eid in baseline_found if eid not in text and eid not in graph]
    still_missing = [eid for eid in expected if eid not in base and eid not in text and eid not in graph]

    return {
        "baseline_found": baseline_found,
        "baseline_missing": baseline_missing,
        "regression_text_found": [eid for eid in expected if eid in text],
        "graph_found": graph_found,
        "recovered_expected": recovered,
        "newly_recalled": newly,
        "lost": lost,
        "text_path_lost": text_path_lost,
        "still_missing": still_missing,
        "graph_unique": graph_unique(expected, base),
        "graph_unique_recovered": bool(recovered),
    }


def truncation_report(
    stats: Mapping[str, Any], *, entries: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Which graph budget stopped selection, if any, and what was kept."""
    hit_limits = [name for name, key in _TRUNCATION_REASONS if stats.get(key)]
    return {
        "truncated": bool(hit_limits),
        "hit_limits": hit_limits,
        "selected": int(stats.get("selected", len(entries))),
        "tokens": int(stats.get("tokens", 0)),
    }


def summarize(rows: list[dict]) -> dict:
    expected_total = sum(len(r["expected"]) for r in rows)
    found_total = sum(len(r["found"]) for r in rows)
    return {
        "items": len(rows),
        "expected_total": expected_total,
        "expected_found": found_total,
        "expected_missing": expected_total - found_total,
        "items_with_any_hit": sum(1 for r in rows if r["hit"]),
        "items_with_all_hits": sum(1 for r in rows if not r["missing"]),
        "items_graph_unique": sum(1 for r in rows if r["graph_unique"]),
    }


def summarize_text(rows: list[dict]) -> dict:
    """Production-parity summary; same fields as the union probe minus its verdict."""
    expected_total = sum(len(r["expected"]) for r in rows)
    found_total = sum(len(r["found"]) for r in rows)
    return {
        "items": len(rows),
        "expected_total": expected_total,
        "expected_found": found_total,
        "expected_missing": expected_total - found_total,
        "items_with_any_hit": sum(1 for r in rows if r["hit"]),
        "items_with_all_hits": sum(1 for r in rows if not r["missing"]),
    }


def summarize_graph(rows: list[dict]) -> dict:
    truncations = [r["truncation"] for r in rows]
    text_found = sum(len(r["regression_text_found"]) for r in rows)
    union = sum(len(r["union_found"]) for r in rows)
    return {
        "items": len(rows),
        "expected_total": sum(len(r["expected"]) for r in rows),
        "baseline_found": sum(len(r["baseline_found"]) for r in rows),
        "regression_text_found": text_found,
        "graph_found": sum(len(r["graph_found"]) for r in rows),
        "graph_only": sum(len(r["graph_only"]) for r in rows),
        "union_found": union,
        "net_gain": union - text_found,
        "recovered_expected": sum(len(r["recovered_expected"]) for r in rows),
        "newly_recalled": sum(len(r["newly_recalled"]) for r in rows),
        "lost": sum(len(r["lost"]) for r in rows),
        "text_path_lost": sum(len(r["text_path_lost"]) for r in rows),
        "still_missing": sum(len(r["still_missing"]) for r in rows),
        "graph_entries": sum(r["graph_entries"] for r in rows),
        "items_graph_unique": sum(1 for r in rows if r["graph_unique"]),
        "items_graph_unique_recovered": sum(1 for r in rows if r["graph_unique_recovered"]),
        "items_truncated": sum(1 for t in truncations if t["truncated"]),
        "truncated_edges": sum(1 for t in truncations if "edges" in t["hit_limits"]),
        "truncated_evidence": sum(1 for t in truncations if "evidence" in t["hit_limits"]),
        "truncated_tokens": sum(1 for t in truncations if "tokens" in t["hit_limits"]),
        "failures": sum(len(r["failures"]) for r in rows),
    }


# ---------------------------------------------------------------------------
# per-item evaluation
# ---------------------------------------------------------------------------


def evaluate_item(item: dict, *, top_k: int) -> dict:
    hits = union_for_queries(item["expanded_queries"], top_k=top_k)
    hit_set = set(hits)
    expected = item["expected_evidence"]
    found = [eid for eid in expected if eid in hit_set]
    missing = [eid for eid in expected if eid not in hit_set]
    return {
        "id": item["id"],
        "category": item["category"],
        "question": item["question"],
        "expected": expected,
        "found": found,
        "missing": missing,
        "hit": bool(found),
        "graph_unique": bool(missing),  # at least one expected unit outside text top-K
        "union_size": len(hits),
        "hits": hits,
    }


def evaluate_text_item(item: dict, *, text_width: int = 14) -> dict:
    """Run one frozen question through the seam with graph expansion off."""
    result = retrieve_evidence(
        item["question"],
        list(item["expanded_queries"]),
        expand_graph=False,
        text_width=text_width,
    )
    hits = _evidence_ids(result.text_hits)
    hit_set = set(hits)
    expected = item["expected_evidence"]
    found = [eid for eid in expected if eid in hit_set]
    missing = [eid for eid in expected if eid not in hit_set]
    return {
        "id": item["id"],
        "category": item["category"],
        "question": item["question"],
        "expected": expected,
        "found": found,
        "missing": missing,
        "hit": bool(found),
        "hits": hits,
        "graph_stats": dict(result.graph_stats),
        "failures": list(result.failures),
        "query_count": result.query_count,
    }


def evaluate_graph_item(
    item: dict,
    *,
    baseline_union: Iterable[str],
    text_ids: Iterable[str] | None = None,
    text_width: int = 14,
) -> dict:
    """Run one frozen question through the shipped retrieval seam.

    ``text_ids`` is the production-parity text arm for the same question; when
    omitted the round's own text hits are used, which must be identical.
    ``text_width`` is passed to the same diversity step as the text arm.
    """
    result = retrieve_evidence(
        item["question"], list(item["expanded_queries"]), text_width=text_width
    )
    text_arm = (
        _evidence_ids(result.text_hits) if text_ids is None else list(text_ids)
    )
    graph_ids = _evidence_ids(result.graph_evidence)
    expected = item["expected_evidence"]
    diff = classify_diff(
        expected,
        baseline_union=baseline_union,
        text_ids=text_arm,
        graph_ids=graph_ids,
    )
    return {
        "id": item["id"],
        "category": item["category"],
        "question": item["question"],
        "expected": expected,
        **diff,
        "graph_only": graph_only(expected, text_ids=text_arm, graph_ids=graph_ids),
        "union_found": union_found(expected, text_ids=text_arm, graph_ids=graph_ids),
        "graph_entries": len(graph_ids),
        "graph_stats": dict(result.graph_stats),
        "truncation": truncation_report(
            result.graph_stats or {}, entries=result.graph_evidence
        ),
        "failures": list(result.failures),
        "query_count": result.query_count,
    }


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def build_text_payload(
    *,
    dataset_path: str,
    dataset: Mapping[str, Any],
    rows: list[dict],
    top_k: int,
    text_width: int,
) -> dict:
    return {
        "dataset": dataset_path,
        "frozen_at": dataset.get("frozen_at"),
        "protocol": dataset.get("retrieval_protocol"),
        "top_k": top_k,
        "text_width": text_width,
        "mode": "text",
        "expand_graph": False,
        "summary": summarize_text(rows),
        "items": rows,
    }


def build_regression_payload(
    *,
    dataset_path: str,
    dataset: Mapping[str, Any],
    baseline: Mapping[str, Any],
    rows: list[dict],
    text_summary: Mapping[str, Any],
    top_k: int,
    text_width: int,
    baseline_reused: bool = False,
) -> dict:
    return {
        "dataset": dataset_path,
        "frozen_at": dataset.get("frozen_at"),
        "protocol": dataset.get("retrieval_protocol"),
        "top_k": top_k,
        "text_width": text_width,
        "mode": "graph",
        "baseline_reused": baseline_reused,
        "baseline_summary": baseline.get("summary"),
        "text_arm_summary": text_summary,
        "summary": summarize_graph(rows),
        "items": rows,
    }


def _display_path(path: Path) -> str:
    """Repo-relative when possible, absolute otherwise (e.g. temp datasets)."""
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def print_table(rows: list[dict], summary: dict) -> None:
    print(f"{'id':8} {'cat':12} {'found':>5} {'exp':>4}  status")
    for r in rows:
        status = "ok" if not r["missing"] else "missing:" + ",".join(e[:8] for e in r["missing"])
        print(f"{r['id']:8} {r['category']:12} {len(r['found']):5} {len(r['expected']):4}  {status}")
    print(
        "\nsummary: "
        f"{summary['expected_found']}/{summary['expected_total']} expected units found; "
        f"{summary['items_with_all_hits']}/{summary['items']} questions fully hit; "
        f"{summary['items_graph_unique']}/{summary['items']} questions have at least one "
        "expected unit outside the text union (graph-unique candidate)"
    )


def print_text_table(rows: list[dict], summary: dict) -> None:
    print(f"{'id':8} {'cat':12} {'found':>5} {'exp':>4} {'hits':>4}  status")
    for r in rows:
        status = "ok" if not r["missing"] else "missing:" + ",".join(e[:8] for e in r["missing"])
        print(
            f"{r['id']:8} {r['category']:12} {len(r['found']):5} "
            f"{len(r['expected']):4} {len(r['hits']):4}  {status}"
        )
    print(
        "\nsummary: "
        f"{summary['expected_found']}/{summary['expected_total']} expected units found "
        "(production-parity text arm, graph off); "
        f"{summary['items_with_all_hits']}/{summary['items']} questions fully hit"
    )


def print_graph_table(rows: list[dict], summary: dict) -> None:
    print(
        f"{'id':8} {'cat':12} {'base':>4} {'text':>4} {'graph':>5} "
        f"{'only':>4} {'new':>3} {'lost':>4} {'trunc':>5}"
    )
    for r in rows:
        trunc = ",".join(r["truncation"]["hit_limits"]) or "-"
        print(
            f"{r['id']:8} {r['category']:12} {len(r['baseline_found']):4} "
            f"{len(r['regression_text_found']):4} {len(r['graph_found']):5} "
            f"{len(r['graph_only']):4} {len(r['newly_recalled']):3} "
            f"{len(r['lost']):4} {trunc:>5}"
        )
    union_delta = summary["newly_recalled"] - summary["lost"]
    print(
        "\nunion-probe delta: "
        f"baseline {summary['baseline_found']}/{summary['expected_total']} expected units; "
        f"new text path {summary['regression_text_found']}; "
        f"graph evidence {summary['graph_found']}; "
        f"newly recalled by graph {summary['newly_recalled']}, "
        f"lost {summary['lost']} (new text path dropped {summary['text_path_lost']}), "
        f"net {union_delta:+d}"
    )
    print(
        "production-parity: "
        f"text arm {summary['regression_text_found']}, "
        f"graph arm union {summary['union_found']} "
        f"(graph evidence {summary['graph_found']}, graph-only {summary['graph_only']}), "
        f"net gain {summary['net_gain']:+d}"
    )
    print(
        "truncations (reported, not a verdict): "
        f"{summary['items_truncated']} questions "
        f"(edges {summary['truncated_edges']}, evidence {summary['truncated_evidence']}, "
        f"tokens {summary['truncated_tokens']}); "
        f"graph-unique questions {summary['items_graph_unique']}, "
        f"recovered by this run {summary['items_graph_unique_recovered']}"
    )


def _positive_int(value: str) -> int:
    """argparse type for a recall width: an integer of at least 1."""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected a positive integer, got {value!r}"
        ) from None
    if number < 1:
        raise argparse.ArgumentTypeError(
            f"expected a positive integer, got {value!r}"
        )
    return number


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    ap.add_argument("--mode", choices=("baseline", "text", "graph"), default="baseline")
    ap.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--top-k", type=int, default=20, help="per-query top-K; 20 matches online recall width")
    ap.add_argument(
        "--text-width",
        type=_positive_int,
        default=14,
        help=(
            "recall width passed to the diversity step; 14 is the production "
            "default. Recorded in every artifact; the union probe has no "
            "diversity step, so its numbers are unchanged."
        ),
    )
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dataset-hash", type=Path, default=DEFAULT_DATASET_HASH)
    ap.add_argument(
        "--reuse-baseline",
        action="store_true",
        help=(
            "graph mode: reuse the existing --baseline JSON instead of running "
            "the baseline, after verifying the frozen dataset SHA-256"
        ),
    )
    args = ap.parse_args()
    require_reuse_mode(args.mode, args.reuse_baseline)

    dataset = load_dataset(args.dataset)
    items = dataset["items"]
    if args.limit:
        items = items[: args.limit]

    defaults = {
        "baseline": DEFAULT_BASELINE_OUT,
        "text": DEFAULT_TEXT_OUT,
        "graph": DEFAULT_GRAPH_OUT,
    }
    out = args.out or defaults[args.mode]
    out = out if out.is_absolute() else (Path.cwd() / out)

    if args.mode == "baseline":
        rows = [evaluate_item(item, top_k=args.top_k) for item in items]
        summary = summarize(rows)
        print_table(rows, summary)
        print(
            f"\ntext width: {args.text_width} "
            "(recorded; the union probe has no diversity step)"
        )
        payload = {
            "dataset": _display_path(args.dataset),
            "frozen_at": dataset.get("frozen_at"),
            "protocol": dataset.get("retrieval_protocol"),
            "top_k": args.top_k,
            "text_width": args.text_width,
            "summary": summary,
            "items": rows,
        }
    elif args.mode == "text":
        rows = [evaluate_text_item(item, text_width=args.text_width) for item in items]
        summary = summarize_text(rows)
        print_text_table(rows, summary)
        print(f"\ntext width: {args.text_width} (diversity top_k)")
        payload = build_text_payload(
            dataset_path=_display_path(args.dataset),
            dataset=dataset,
            rows=rows,
            top_k=args.top_k,
            text_width=args.text_width,
        )
    else:
        baseline_reused = False
        dataset_digest = None
        if args.reuse_baseline:
            dataset_digest = verify_frozen_dataset(
                args.dataset, sidecar=args.dataset_hash
            )
            baseline_reused = True
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        baseline_rows = {row["id"]: row for row in baseline.get("items", [])}
        text_rows = [
            evaluate_text_item(item, text_width=args.text_width) for item in items
        ]
        text_by_id = {row["id"]: row for row in text_rows}
        rows = []
        for item in items:
            base_row = baseline_rows.get(item["id"])
            if base_row is None:
                raise SystemExit(
                    f"baseline has no row for {item['id']}; rerun with --mode baseline first"
                )
            rows.append(
                evaluate_graph_item(
                    item,
                    baseline_union=base_row["hits"],
                    text_ids=text_by_id[item["id"]]["hits"],
                    text_width=args.text_width,
                )
            )
        payload = build_regression_payload(
            dataset_path=_display_path(args.dataset),
            dataset=dataset,
            baseline=baseline,
            rows=rows,
            text_summary=summarize_text(text_rows),
            top_k=args.top_k,
            text_width=args.text_width,
            baseline_reused=baseline_reused,
        )
        print_graph_table(rows, payload["summary"])
        print(f"\ntext width: {args.text_width} (diversity top_k, both arms)")
        if baseline_reused:
            print(
                f"\nbaseline: reused verified artifact {_display_path(args.baseline)} "
                f"(dataset sha256 {dataset_digest})"
            )

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        shown = out.resolve().relative_to(ROOT)
    except ValueError:
        shown = out
    print(f"\nwrote {shown}")


if __name__ == "__main__":
    main()
