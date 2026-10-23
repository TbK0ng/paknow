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

Usage:
    python scripts/eval_answers.py
    python scripts/eval_answers.py --limit 4 --out .scratch/retrieval-eval-parity/answers/dev
    python scripts/eval_answers.py --resume --out <same dir as the interrupted run>
"""
from __future__ import annotations

import argparse
import hashlib
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


def _display_path(path: Path) -> str:
    """Repo-relative when possible, absolute otherwise (temp datasets)."""
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def dataset_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def select_items(dataset: Mapping[str, Any], limit: int = 0) -> list[dict]:
    items = list(dataset["items"])
    return items[:limit] if limit else items


# ---------------------------------------------------------------------------
# deterministic checks (pure)
# ---------------------------------------------------------------------------


def citation_checks(
    cited_ids: Sequence[str],
    citable_ids: Iterable[str],
    graph_ids: Iterable[str],
) -> dict[str, Any]:
    """Citation hard constraints: in the citable set, and never graph-only."""
    citable = set(citable_ids)
    graph = set(graph_ids)
    invalid = [eid for eid in cited_ids if eid not in citable]
    graph_citations = [eid for eid in cited_ids if eid in graph]
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


def error_record(item: Mapping[str, Any], arm: str, error: str, elapsed: float) -> dict:
    return {
        "id": item["id"],
        "arm": arm,
        "category": item.get("category"),
        "question": item["question"],
        "status": "error",
        "error": error,
        "answer": "",
        "cited_evidence_ids": [],
        "citable_ids": [],
        "graph_evidence_ids": [],
        "routes": [],
        "rounds": 0,
        "failures": [error],
        "expected_evidence": list(item.get("expected_evidence", [])),
        "invalid_citations": [],
        "graph_citations": [],
        "hard_pass": False,
        "expected_covered": [],
        "expected_missing": list(item.get("expected_evidence", [])),
        "coverage": 0.0,
        "coverage_complete": False,
        "elapsed_s": round(elapsed, 3),
    }


def run_item(
    item: Mapping[str, Any],
    arm: str,
    run_query: Callable[..., Mapping[str, Any]],
) -> dict:
    """One stateless production-chain call plus the deterministic constraints."""
    expand_graph = dict(ARMS)[arm]
    started = time.monotonic()
    try:
        # generation sees the question and its own retrieval result only; the
        # expected evidence below is never passed into the chain
        result = run_query(item["question"], expand_graph=expand_graph)
    except Exception as e:  # noqa: BLE001
        return error_record(item, arm, f"{type(e).__name__}: {e}", time.monotonic() - started)

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
        "id": item["id"],
        "arm": arm,
        "category": item.get("category"),
        "question": item["question"],
        "status": "ok",
        "answer": answer.get("answer") or "",
        "cited_evidence_ids": cited,
        "citable_ids": citable,
        "graph_evidence_ids": graph_ids,
        "routes": list(result.get("routes") or []),
        "rounds": int(result.get("rounds") or 0),
        "failures": list(result.get("failures") or []),
        "expected_evidence": expected,
        **checks,
        **coverage,
        "elapsed_s": round(elapsed, 3),
    }


# ---------------------------------------------------------------------------
# checkpoint / resume
# ---------------------------------------------------------------------------


def record_key(record: Mapping[str, Any]) -> tuple[str, str]:
    return (str(record["id"]), str(record["arm"]))


def load_checkpoint(path: Path) -> dict[tuple[str, str], dict]:
    """Latest record per (item, arm); the last line wins."""
    latest: dict[tuple[str, str], dict] = {}
    if not path.exists():
        return latest
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            # a killed process may leave a partial trailing line
            continue
        latest[record_key(record)] = record
    return latest


def append_checkpoint(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()


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


def write_records(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_payload(
    *,
    dataset_path: Path,
    digest: str,
    records: Sequence[Mapping[str, Any]],
    limit: int,
    timestamp: str,
) -> dict:
    delta = summarize_delta(records)
    errors = [
        {"id": r["id"], "arm": r["arm"], "error": r.get("error")}
        for r in records
        if r.get("status") != "ok"
    ]
    return {
        "dataset": _display_path(dataset_path),
        "dataset_sha256": digest,
        "timestamp": timestamp,
        "limit": limit,
        "arms": {name: {"expand_graph": flag} for name, flag in ARMS},
        "summary": delta,
        "errors": errors,
    }


def print_summary(payload: Mapping[str, Any]) -> None:
    summary = payload["summary"]
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
    args = ap.parse_args()

    from paknow.workflows.graph import run_query  # noqa: E402

    if args.resume and args.out is None:
        ap.error("--resume needs --out pointing at the interrupted run's directory")
    out_dir = args.out or _default_out()
    out_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = out_dir / "checkpoint.jsonl"

    dataset = load_dataset(args.dataset)
    items = select_items(dataset, args.limit)
    digest = dataset_digest(args.dataset)
    checkpoint = load_checkpoint(checkpoint_path) if args.resume else {}
    todo = pending_keys(items, ARMS, checkpoint)

    started = time.monotonic()
    for index, (item, arm) in enumerate(todo, 1):
        record = run_item(item, arm, run_query)
        append_checkpoint(checkpoint_path, record)
        checkpoint[record_key(record)] = record
        status = "ok" if record["status"] == "ok" else "ERROR"
        print(
            f"[{index}/{len(todo)}] {item['id']} {arm} {status} "
            f"{record['elapsed_s']:.1f}s",
            flush=True,
        )
    wall = time.monotonic() - started

    records = merge_records(items, ARMS, checkpoint)
    timestamp = out_dir.name
    payload = build_payload(
        dataset_path=args.dataset,
        digest=digest,
        records=records,
        limit=args.limit,
        timestamp=timestamp,
    )
    payload["wall_time_s"] = round(wall, 1)
    payload["ran"] = len(todo)
    write_records(out_dir / "records.jsonl", records)
    (out_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print_summary(payload)
    print(f"\nwrote {out_dir} (ran {len(todo)} of {len(items) * len(ARMS)} arm-items)")


if __name__ == "__main__":
    main()
