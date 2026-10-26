"""Offline answer-level evaluation over the v2 gold dataset.

Each question runs once per arm through the full production chain
(``run_query``), with a stateless call per (item, arm):

- ``text``  — ``expand_graph=False``: the production-parity text channel.
- ``graph`` — ``expand_graph=True``: the shipped chain.

Every other parameter is identical, so any answer difference is attributable to
the graph layer. Generation only ever sees the question and its own retrieval
result; the expected evidence and the gold answer points stay in this runner.

Per item the runner records the answer, the cited evidence ids, the exact
citable set generation rendered, the graph evidence ids, routes, rounds and the
retrieval/generation failures. It then evaluates the deterministic constraints:

- every cited id is in that answer's citable set;
- no graph evidence id is cited (graph entries are understanding aids, never
  author quotes);
- expected-evidence coverage is computed per item (covered / missing); it is
  reported alongside the hard verdict rather than folded into a score.

Checkpointing is per (item, arm): one JSON line is appended as soon as an arm
finishes, so ``--resume`` only reruns what is missing or errored. A failed item
is recorded and the batch continues.

``--frozen-queries`` builds each item's plan from the gold item itself (its
``question`` plus ``expanded_queries``) and injects it, so the router model is
never asked: same code plus same dataset must reproduce the same query list,
rounds and coverage. Every record carries ``plan_source`` (``frozen`` /
``routed``), the assembled ``queries`` and their ``query_hash``; the summary
carries the dataset hash, so an edited dataset or plan shows up immediately.

Usage:
    python scripts/eval_answers.py
    python scripts/eval_answers.py --limit 4 --out .scratch/retrieval-eval-parity/answers/dev
    python scripts/eval_answers.py --frozen-queries --out .scratch/retrieval-eval-parity/answers/frozen
    python scripts/eval_answers.py --resume --out <same dir as the interrupted run>
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.eval_common import (  # noqa: E402
    append_checkpoint,
    display_path,
    file_sha256,
    load_checkpoint,
    record_key,
    write_jsonl,
)

from paknow.workflows.router import QueryPlan  # noqa: E402

DEFAULT_DATASET = ROOT / "evals" / "gold" / "graph_expansion_v2.json"
ANSWERS_DIR = ROOT / ".scratch" / "retrieval-eval-parity" / "answers"

# arm name -> ``expand_graph``; order is fixed (text first, then graph)
ARMS: tuple[tuple[str, bool], ...] = (("text", False), ("graph", True))

COVERAGE_BUCKETS = ("0", "(0,0.5)", "[0.5,1)", "1")


# ---------------------------------------------------------------------------
# dataset
# ---------------------------------------------------------------------------


def load_dataset(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def select_items(dataset: Mapping[str, Any], limit: int = 0) -> list[dict]:
    items = list(dataset["items"])
    return items[:limit] if limit else items


def frozen_plan(item: Mapping[str, Any]) -> QueryPlan:
    """The gold item's own queries as a plan, so routing never runs.

    ``expanded_queries[0]`` is the question itself (dataset contract); the rest
    become sub-queries, so ``retrieval_queries`` rebuilds that exact list. One
    round only: a judge-driven gap round would prepend a model-produced query,
    so rounds and coverage would stop being a property of the dataset.
    """
    return QueryPlan(
        sub_queries=list(item.get("expanded_queries") or [])[1:],
        max_search_rounds=1,
    )


# ---------------------------------------------------------------------------
# deterministic checks (pure)
# ---------------------------------------------------------------------------


def citation_checks(
    cited_ids: Sequence[str],
    citable_ids: Iterable[str],
    graph_ids: Iterable[str],
) -> dict[str, Any]:
    """Citation hard constraints: in the citable set, and never graph-only.

    A graph entry may point at evidence that is also a citable text unit (the
    graph block renders it as ``已见 [n]``); citing that id is citing the
    author's text, not the graph's synthesis. Only ids that exist *solely* as
    graph evidence are forbidden.
    """
    citable = set(citable_ids)
    graph_only = set(graph_ids) - citable
    invalid = [eid for eid in cited_ids if eid not in citable]
    graph_citations = [eid for eid in cited_ids if eid in graph_only]
    return {
        "invalid_citations": invalid,
        "graph_citations": graph_citations,
        "citations_valid": not invalid,
        "no_graph_citations": not graph_citations,
        "hard_pass": not invalid and not graph_citations,
    }


def expected_coverage(
    expected_ids: Sequence[str], citable_ids: Iterable[str]
) -> dict[str, Any]:
    """How much expected evidence the answer's citable set actually reached."""
    citable = set(citable_ids)
    covered = [eid for eid in expected_ids if eid in citable]
    missing = [eid for eid in expected_ids if eid not in citable]
    total = len(expected_ids)
    return {
        "expected_covered": covered,
        "expected_missing": missing,
        "coverage": (len(covered) / total) if total else 0.0,
        "coverage_complete": bool(total) and not missing,
    }


def coverage_bucket(ratio: float) -> str:
    if ratio <= 0:
        return "0"
    if ratio < 0.5:
        return "(0,0.5)"
    if ratio < 1:
        return "[0.5,1)"
    return "1"


# ---------------------------------------------------------------------------
# per-item run
# ---------------------------------------------------------------------------


def _record(item: Mapping[str, Any], arm: str, plan_source: str) -> dict:
    """The fields both a successful and an errored arm always carry."""
    expected = list(item.get("expected_evidence", []))
    return {
        "id": item["id"],
        "arm": arm,
        "category": item.get("category"),
        "question": item["question"],
        # which plan produced the query list, and that list's hash: the two
        # runs are comparable only when both match
        "plan_source": plan_source,
        "queries": [],
        "query_hash": "",
        "answer": "",
        "degraded": False,
        "degradation": None,
        "cited_evidence_ids": [],
        "citable_ids": [],
        "graph_evidence_ids": [],
        "routes": [],
        "rounds": 0,
        "failures": [],
        "expected_evidence": expected,
        "invalid_citations": [],
        "graph_citations": [],
        "hard_pass": False,
        "expected_covered": [],
        "expected_missing": list(expected),
        "coverage": 0.0,
        "coverage_complete": False,
    }


def error_record(
    item: Mapping[str, Any], arm: str, error: str, elapsed: float, plan_source: str
) -> dict:
    return {
        **_record(item, arm, plan_source),
        "status": "error",
        "error": error,
        "failures": [error],
        "elapsed_s": round(elapsed, 3),
    }


def run_item(
    item: Mapping[str, Any],
    arm: str,
    run_query: Callable[..., Mapping[str, Any]],
    *,
    frozen: bool = False,
) -> dict:
    """One stateless production-chain call plus the deterministic constraints.

    ``frozen`` injects the item's own plan (``--frozen-queries``): the router
    model is not consulted, so the query list is a property of the dataset.
    """
    expand_graph = dict(ARMS)[arm]
    plan_source = "frozen" if frozen else "routed"
    plan = frozen_plan(item) if frozen else None
    started = time.monotonic()
    try:
        # generation sees the question and its own retrieval result only; the
        # expected evidence below is never passed into the chain
        result = run_query(item["question"], expand_graph=expand_graph, plan=plan)
    except Exception as e:  # noqa: BLE001
        return error_record(
            item, arm, f"{type(e).__name__}: {e}", time.monotonic() - started, plan_source
        )

    elapsed = time.monotonic() - started
    answer = result.get("answer") or {}
    cited = list(answer.get("evidence_ids") or [])
    citable = list(result.get("citable_ids") or [])
    graph_ids = [
        str(entry["evidence_id"])
        for entry in result.get("graph_evidence") or []
        if entry.get("evidence_id")
    ]
    expected = list(item.get("expected_evidence") or [])
    checks = citation_checks(cited, citable, graph_ids)
    coverage = expected_coverage(expected, citable)
    return {
        **_record(item, arm, plan_source),
        "status": "ok",
        # the chain's own provenance wins over the flag we passed
        "plan_source": result.get("plan_source") or plan_source,
        "queries": list(result.get("queries") or []),
        "query_hash": result.get("query_hash") or "",
        "answer": answer.get("answer") or "",
        # a degraded answer (generation failed) stays distinguishable from a
        # genuinely short one, so the gate can reject empty answers
        "degraded": bool(answer.get("degraded")),
        "degradation": answer.get("degradation"),
        "cited_evidence_ids": cited,
        "citable_ids": citable,
        "graph_evidence_ids": graph_ids,
        "routes": list(result.get("routes") or []),
        "rounds": int(result.get("rounds") or 0),
        "failures": list(result.get("failures") or []),
        **checks,
        **coverage,
        "elapsed_s": round(elapsed, 3),
    }


def refresh_checks(record: Mapping[str, Any]) -> dict:
    """Recompute deterministic fields from the stored ids (pure, idempotent)."""
    record = dict(record)
    if record.get("status") != "ok":
        return record
    record.update(
        citation_checks(
            record.get("cited_evidence_ids") or [],
            record.get("citable_ids") or [],
            record.get("graph_evidence_ids") or [],
        )
    )
    record.update(
        expected_coverage(
            record.get("expected_evidence") or [],
            record.get("citable_ids") or [],
        )
    )
    return record


# ---------------------------------------------------------------------------
# checkpoint / resume
# ---------------------------------------------------------------------------


def merge_records(
    items: Sequence[Mapping[str, Any]],
    arms: Sequence[tuple[str, bool]],
    checkpoint: Mapping[tuple[str, str], dict],
) -> list[dict]:
    """Stable item-major, arm-minor order, latest record per key."""
    merged: list[dict] = []
    for item in items:
        for arm, _ in arms:
            record = checkpoint.get((str(item["id"]), arm))
            if record is not None:
                merged.append(record)
    return merged


def pending_keys(
    items: Sequence[Mapping[str, Any]],
    arms: Sequence[tuple[str, bool]],
    checkpoint: Mapping[tuple[str, str], dict],
) -> list[tuple[dict, str]]:
    """Items to run: missing arms plus arms whose last attempt errored."""
    todo: list[tuple[dict, str]] = []
    for item in items:
        for arm, _ in arms:
            record = checkpoint.get((str(item["id"]), arm))
            if record is None or record.get("status") != "ok":
                todo.append((dict(item), arm))
    return todo


# ---------------------------------------------------------------------------
# summaries (pure)
# ---------------------------------------------------------------------------


def summarize_arm(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ok = [r for r in records if r.get("status") == "ok"]
    total = len(records)
    errors = total - len(ok)
    hard = sum(1 for r in ok if r.get("hard_pass"))
    buckets: Counter[str] = Counter(coverage_bucket(float(r.get("coverage") or 0)) for r in ok)
    coverage_values = [float(r.get("coverage") or 0) for r in ok]
    return {
        "items": total,
        "ok": len(ok),
        "errors": errors,
        "hard_pass": hard,
        "hard_pass_rate": round(hard / len(ok), 4) if ok else 0.0,
        "invalid_citation_items": sum(1 for r in ok if r.get("invalid_citations")),
        "graph_citation_items": sum(1 for r in ok if r.get("graph_citations")),
        "coverage_mean": round(sum(coverage_values) / len(ok), 4) if ok else 0.0,
        "coverage_complete_items": sum(1 for r in ok if r.get("coverage_complete")),
        "coverage_distribution": {name: buckets.get(name, 0) for name in COVERAGE_BUCKETS},
        "citations_total": sum(len(r.get("cited_evidence_ids") or []) for r in ok),
        "citable_total": sum(len(r.get("citable_ids") or []) for r in ok),
        "graph_evidence_total": sum(len(r.get("graph_evidence_ids") or []) for r in ok),
        "rounds_mean": round(
            sum(int(r.get("rounds") or 0) for r in ok) / len(ok), 4
        )
        if ok
        else 0.0,
        "retrieval_failures": sum(len(r.get("failures") or []) for r in ok),
        "elapsed_s": round(sum(float(r.get("elapsed_s") or 0) for r in records), 1),
    }


def summarize_delta(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Graph minus text on paired items, plus the per-arm summaries."""
    by_key = {(r["id"], r["arm"]): r for r in records}
    ids = list(dict.fromkeys(r["id"] for r in records))
    paired = [
        (by_key[(i, "text")], by_key[(i, "graph")])
        for i in ids
        if (i, "text") in by_key and (i, "graph") in by_key
    ]
    both_ok = [
        (t, g)
        for t, g in paired
        if t.get("status") == "ok" and g.get("status") == "ok"
    ]

    def arm(name: str) -> dict:
        return summarize_arm([by_key[(i, name)] for i in ids if (i, name) in by_key])

    text = arm("text")
    graph = arm("graph")
    better = sum(1 for t, g in both_ok if float(g["coverage"]) > float(t["coverage"]))
    worse = sum(1 for t, g in both_ok if float(g["coverage"]) < float(t["coverage"]))
    paired_n = len(both_ok)
    paired_hard_text = sum(1 for t, _ in both_ok if t.get("hard_pass"))
    paired_hard_graph = sum(1 for _, g in both_ok if g.get("hard_pass"))
    paired_cov_text = (
        sum(float(t.get("coverage") or 0) for t, _ in both_ok) / paired_n if paired_n else 0.0
    )
    paired_cov_graph = (
        sum(float(g.get("coverage") or 0) for _, g in both_ok) / paired_n if paired_n else 0.0
    )
    return {
        "text": text,
        "graph": graph,
        "paired": paired_n,
        "hard_pass_delta": paired_hard_graph - paired_hard_text,
        "hard_pass_rate_delta": round(
            (paired_hard_graph - paired_hard_text) / paired_n, 4
        )
        if paired_n
        else 0.0,
        "coverage_mean_delta": round(paired_cov_graph - paired_cov_text, 4),
        "coverage_better_items": better,
        "coverage_worse_items": worse,
        "coverage_equal_items": paired_n - better - worse,
        "citations_total_delta": graph["citations_total"] - text["citations_total"],
        "rounds_mean_delta": round(graph["rounds_mean"] - text["rounds_mean"], 4),
    }


# ---------------------------------------------------------------------------
# artifacts
# ---------------------------------------------------------------------------


def build_payload(
    *,
    dataset_path: Path,
    digest: str,
    records: Sequence[Mapping[str, Any]],
    limit: int,
    timestamp: str,
    plan_source: str = "routed",
) -> dict:
    delta = summarize_delta(records)
    errors = [
        {"id": r["id"], "arm": r["arm"], "error": r.get("error")}
        for r in records
        if r.get("status") != "ok"
    ]
    return {
        "dataset": display_path(dataset_path),
        "dataset_sha256": digest,
        "timestamp": timestamp,
        "limit": limit,
        "plan_source": plan_source,
        "arms": {name: {"expand_graph": flag} for name, flag in ARMS},
        "summary": delta,
        "errors": errors,
    }


def print_summary(payload: Mapping[str, Any]) -> None:
    summary = payload["summary"]
    print(
        f"plan: {payload['plan_source']} "
        f"dataset {payload['dataset_sha256'][:12]} ({payload['dataset']})"
    )
    for name in ("text", "graph"):
        s = summary[name]
        print(
            f"{name:5} items={s['items']} ok={s['ok']} errors={s['errors']} "
            f"hard {s['hard_pass']}/{s['ok']} ({s['hard_pass_rate']:.0%}) "
            f"coverage {s['coverage_mean']:.2f} complete={s['coverage_complete_items']} "
            f"dist={s['coverage_distribution']} rounds~{s['rounds_mean']:.2f}"
        )
    print(
        "delta graph-text: "
        f"hard {summary['hard_pass_delta']:+d} "
        f"({summary['hard_pass_rate_delta']:+.1%}), "
        f"coverage {summary['coverage_mean_delta']:+.3f} "
        f"(better {summary['coverage_better_items']}, "
        f"worse {summary['coverage_worse_items']}, "
        f"equal {summary['coverage_equal_items']} of {summary['paired']})"
    )
    if payload["errors"]:
        print(f"errors: {len(payload['errors'])} (recorded, resumed on next --resume)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_out() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ANSWERS_DIR / stamp


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    ap.add_argument("--out", type=Path, default=None, help="artifact directory (UTC timestamp by default)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--resume", action="store_true", help="reuse the --out checkpoint and run missing arms")
    ap.add_argument(
        "--frozen-queries",
        action="store_true",
        help="build each plan from the gold item's question + expanded_queries (no routing)",
    )
    args = ap.parse_args()

    from paknow.workflows.graph import run_query  # noqa: E402

    if args.resume and args.out is None:
        ap.error("--resume needs --out pointing at the interrupted run's directory")
    plan_source = "frozen" if args.frozen_queries else "routed"
    out_dir = args.out or _default_out()
    out_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = out_dir / "checkpoint.jsonl"

    dataset = load_dataset(args.dataset)
    items = select_items(dataset, args.limit)
    digest = file_sha256(args.dataset)
    checkpoint = load_checkpoint(checkpoint_path) if args.resume else {}
    todo = pending_keys(items, ARMS, checkpoint)

    started = time.monotonic()
    for index, (item, arm) in enumerate(todo, 1):
        record = run_item(item, arm, run_query, frozen=args.frozen_queries)
        append_checkpoint(checkpoint_path, record)
        checkpoint[record_key(record)] = record
        status = "ok" if record["status"] == "ok" else "ERROR"
        print(
            f"[{index}/{len(todo)}] {item['id']} {arm} {status} "
            f"{record['elapsed_s']:.1f}s",
            flush=True,
        )
    wall = time.monotonic() - started

    # recompute the deterministic fields from the stored ids so artifacts stay
    # consistent when the constraint definition is tightened between runs
    records = [refresh_checks(r) for r in merge_records(items, ARMS, checkpoint)]
    timestamp = out_dir.name
    payload = build_payload(
        dataset_path=args.dataset,
        digest=digest,
        records=records,
        limit=args.limit,
        timestamp=timestamp,
        plan_source=plan_source,
    )
    summary_path = out_dir / "summary.json"
    previous_wall = 0.0
    if summary_path.exists():
        try:
            previous_wall = float(json.loads(summary_path.read_text(encoding="utf-8")).get("wall_time_s") or 0)
        except (ValueError, json.JSONDecodeError):
            previous_wall = 0.0
    payload["wall_time_s"] = round(previous_wall + wall, 1)
    payload["chain_time_s"] = round(
        sum(float(r.get("elapsed_s") or 0) for r in records), 1
    )
    payload["ran"] = len(todo)
    write_jsonl(out_dir / "records.jsonl", records)
    (out_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print_summary(payload)
    print(f"\nwrote {out_dir} (ran {len(todo)} of {len(items) * len(ARMS)} arm-items)")


if __name__ == "__main__":
    main()
