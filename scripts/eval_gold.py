"""Offline gold-standard evaluation for graph expansion.

Runs the 20 frozen gold questions against retrieval and reports, per question,
whether each expected evidence unit appears in the union of the six frozen
queries' top-K results. Queries are read from the dataset and never regenerated,
so baseline and regression runs are comparable.

This is an offline evaluation, not a CI gate: it needs the text index and the
embedding model. It does not call the routing model.

Usage:
    python scripts/eval_gold.py
    python scripts/eval_gold.py --limit 4 --out .scratch/graph-expansion/baseline.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paknow.retrieval.search import hybrid_search  # noqa: E402

DEFAULT_DATASET = ROOT / "evals" / "gold" / "graph_expansion.json"


def load_dataset(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def union_for_queries(queries: list[str], *, top_k: int) -> list[str]:
    """Top-K union across frozen queries, preserving first-seen order."""
    seen: dict[str, None] = {}
    for q in queries:
        for point in hybrid_search(q, top_k=top_k):
            eid = (point.payload or {}).get("evidence_id")
            if eid:
                seen.setdefault(eid, None)
    return list(seen)


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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    ap.add_argument("--out", type=Path, default=ROOT / ".scratch" / "graph-expansion" / "gold-baseline.json")
    ap.add_argument("--top-k", type=int, default=20, help="per-query top-K; 20 matches online recall width")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    dataset = load_dataset(args.dataset)
    items = dataset["items"]
    if args.limit:
        items = items[: args.limit]

    rows = [evaluate_item(item, top_k=args.top_k) for item in items]
    summary = summarize(rows)
    print_table(rows, summary)

    payload = {
        "dataset": str(args.dataset.relative_to(ROOT)),
        "frozen_at": dataset.get("frozen_at"),
        "protocol": dataset.get("retrieval_protocol"),
        "top_k": args.top_k,
        "summary": summary,
        "items": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {args.out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
