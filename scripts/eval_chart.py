"""Chart golden evaluation: one image per item, three deterministic rates.

Each item loads its image (repo-relative path + SHA-256), runs the production
chain once through ``run_query(question, images=[data], expand_graph=False)``
and judges the structured ``chart_read`` against the item's expected levels:

- miss   — expected price the read did not surface;
- extra  — read price with no expected counterpart;
- drift  — read price within the same integer part but not equal.

Exact equality, no tolerance. The three buckets are disjoint, so drift never
also counts as a hit or a miss. No blind judge is called; the judgement is pure.

A missing image or a SHA-256 mismatch records ``status: "skipped"`` and the
chain is never called. Skipped items are excluded from every rate denominator
and counted separately, so a checkout without the images still runs.

Usage:
    python scripts/eval_chart.py --limit 2 --out .scratch/chart-analysis/eval/dev
    python scripts/eval_chart.py
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

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

DEFAULT_DATASET = ROOT / "evals" / "gold" / "chart_v1.json"
EVAL_DIR = ROOT / ".scratch" / "chart-analysis" / "eval"

ARM = "chart"

ImageLoader = Callable[[Mapping[str, Any]], "tuple[bytes | None, str | None]"]


# ---------------------------------------------------------------------------
# deterministic judgement (pure)
# ---------------------------------------------------------------------------


def judge_levels(
    expected: Sequence[float], observed: Sequence[float]
) -> dict[str, Any]:
    """Three disjoint price buckets plus their rates and denominators.

    Exact multiset matching runs before drift pairing: a surfaced level that
    equals an expected level is a hit, never a drift candidate for another
    expected level. Drift is then "same integer part, not equal" (the observed
    7499.25/7499.75 and 7489.15/7489.75 offsets); among several candidates the
    nearest unused one wins. Denominators differ per rate and are returned:
    miss = missed/expected, extra = extra/observed,
    drift = drifted/(matched+drifted) — i.e. of the levels actually surfaced.
    """
    remaining = list(observed)
    matched: list[float] = []
    unmatched: list[float] = []
    for price in expected:
        if price in remaining:
            remaining.remove(price)
            matched.append(price)
        else:
            unmatched.append(price)
    missed: list[float] = []
    drifted: list[dict[str, float]] = []
    for price in unmatched:
        candidates = [o for o in remaining if math.floor(o) == math.floor(price)]
        if not candidates:
            missed.append(price)
            continue
        near = min(candidates, key=lambda value: (abs(value - price), value))
        remaining.remove(near)
        drifted.append({"expected": price, "observed": near})
    extra = sorted(remaining)
    surfaced = len(matched) + len(drifted)
    return {
        "matched_levels": matched,
        "missed_levels": missed,
        "extra_levels": extra,
        "drifted_levels": drifted,
        "miss_rate": round(len(missed) / len(expected), 4) if expected else 0.0,
        "extra_rate": round(len(extra) / len(observed), 4) if observed else 0.0,
        "drift_rate": round(len(drifted) / surfaced, 4) if surfaced else 0.0,
    }


def observed_prices(chart_read: Mapping[str, Any] | None) -> list[float]:
    """Readable numeric level prices; an uncertain digit is ``None``, not a miss."""
    levels = (chart_read or {}).get("levels") or []
    return [float(level["price"]) for level in levels if level.get("price") is not None]


# ---------------------------------------------------------------------------
# per-item run
# ---------------------------------------------------------------------------


def load_image(item: Mapping[str, Any]) -> tuple[bytes | None, str | None]:
    """Image bytes after the hash gate, or ``(None, reason)`` when unusable."""
    path = ROOT / item["image"]
    if not path.is_file():
        return None, "image_missing"
    if file_sha256(path) != item["image_sha256"]:
        return None, "image_hash_mismatch"
    return path.read_bytes(), None


def _expected_prices(item: Mapping[str, Any]) -> list[float]:
    return [
        float(level["price"])
        for level in item.get("expected_levels") or []
        if level.get("price") is not None
    ]


def _base_record(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "arm": ARM,
        "question": item["question"],
        "image": item["image"],
        "image_sha256": item["image_sha256"],
        "expected_levels": _expected_prices(item),
        "expected_swings": list(item.get("expected_swings") or []),
        "expected_state": item.get("expected_state"),
        "expected_unreadable": list(item.get("unreadable") or []),
        "chart_read": None,
        "chart_read_status": None,
        "observed_state": None,
        "matched_levels": [],
        "missed_levels": [],
        "extra_levels": [],
        "drifted_levels": [],
        "miss_rate": 0.0,
        "extra_rate": 0.0,
        "drift_rate": 0.0,
        "failures": [],
    }


def skipped_record(item: Mapping[str, Any], reason: str) -> dict:
    return {**_base_record(item), "status": "skipped", "reason": reason, "elapsed_s": 0.0}


def error_record(item: Mapping[str, Any], error: str, elapsed: float) -> dict:
    return {
        **_base_record(item),
        "status": "error",
        "error": error,
        "failures": [error],
        "elapsed_s": round(elapsed, 3),
    }


def run_item(
    item: Mapping[str, Any],
    run_query: Callable[..., Mapping[str, Any]],
    *,
    image_loader: ImageLoader = load_image,
) -> dict:
    """One chain call, then the pure judgement against the expected levels."""
    data, reason = image_loader(item)
    if data is None:
        return skipped_record(item, reason or "image_missing")
    started = time.monotonic()
    try:
        result = run_query(item["question"], images=[data], expand_graph=False)
    except Exception as e:  # noqa: BLE001
        return error_record(item, f"{type(e).__name__}: {e}", time.monotonic() - started)
    elapsed = time.monotonic() - started
    chart_read = result.get("chart_read")
    observed = observed_prices(chart_read)
    judgement = judge_levels(_expected_prices(item), observed)
    return {
        **_base_record(item),
        **judgement,
        "status": "ok",
        "chart_read": chart_read,
        "chart_read_status": result.get("chart_read_status"),
        "observed_state": (chart_read or {}).get("market_state"),
        "observed_levels": observed,
        "failures": list(result.get("failures") or []),
        "elapsed_s": round(elapsed, 3),
    }


# ---------------------------------------------------------------------------
# checkpoint / resume
# ---------------------------------------------------------------------------


def merge_records(
    items: Sequence[Mapping[str, Any]],
    checkpoint: Mapping[tuple[str, str], dict],
) -> list[dict]:
    merged: list[dict] = []
    for item in items:
        record = checkpoint.get((str(item["id"]), ARM))
        if record is not None:
            merged.append(record)
    return merged


def pending_items(
    items: Sequence[Mapping[str, Any]],
    checkpoint: Mapping[tuple[str, str], dict],
) -> list[dict]:
    """Missing items plus errored ones; skipped is a verdict, not a retry."""
    todo: list[dict] = []
    for item in items:
        record = checkpoint.get((str(item["id"]), ARM))
        if record is None or record.get("status") == "error":
            todo.append(dict(item))
    return todo


# ---------------------------------------------------------------------------
# summary (pure)
# ---------------------------------------------------------------------------


def summarize(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ok = [r for r in records if r.get("status") == "ok"]
    skipped = [r for r in records if r.get("status") == "skipped"]
    errors = [r for r in records if r.get("status") == "error"]
    matched = sum(len(r.get("matched_levels") or []) for r in ok)
    missed = sum(len(r.get("missed_levels") or []) for r in ok)
    extra = sum(len(r.get("extra_levels") or []) for r in ok)
    drifted = sum(len(r.get("drifted_levels") or []) for r in ok)
    expected_total = sum(len(r.get("expected_levels") or []) for r in ok)
    answered_total = sum(len(r.get("observed_levels") or []) for r in ok)
    surfaced = matched + drifted
    return {
        "items": len(records),
        "ok": len(ok),
        "skipped": len(skipped),
        "errors": len(errors),
        "skip_reasons": dict(Counter(r.get("reason") for r in skipped)),
        "items_with_miss": sum(1 for r in ok if r.get("missed_levels")),
        "items_with_extra": sum(1 for r in ok if r.get("extra_levels")),
        "items_with_drift": sum(1 for r in ok if r.get("drifted_levels")),
        "expected_levels_total": expected_total,
        "answered_levels_total": answered_total,
        "matched_total": matched,
        "missed_total": missed,
        "extra_total": extra,
        "drifted_total": drifted,
        "miss_rate": round(missed / expected_total, 4) if expected_total else 0.0,
        "extra_rate": round(extra / answered_total, 4) if answered_total else 0.0,
        "drift_rate": round(drifted / surfaced, 4) if surfaced else 0.0,
        "elapsed_s": round(sum(float(r.get("elapsed_s") or 0) for r in records), 1),
    }


def build_payload(
    *,
    dataset_path: Path,
    digest: str,
    records: Sequence[Mapping[str, Any]],
    limit: int,
    timestamp: str,
) -> dict:
    return {
        "dataset": display_path(dataset_path),
        "dataset_sha256": digest,
        "timestamp": timestamp,
        "limit": limit,
        "arms": {ARM: {"expand_graph": False}},
        "summary": summarize(records),
        "details": [
            {
                "id": r["id"],
                "status": r.get("status"),
                "reason": r.get("reason"),
                "missed_levels": r.get("missed_levels") or [],
                "extra_levels": r.get("extra_levels") or [],
                "drifted_levels": r.get("drifted_levels") or [],
            }
            for r in records
        ],
        "errors": [
            {"id": r["id"], "error": r.get("error")}
            for r in records
            if r.get("status") == "error"
        ],
    }


def print_summary(payload: Mapping[str, Any]) -> None:
    s = payload["summary"]
    print(
        f"chart items={s['items']} ok={s['ok']} skipped={s['skipped']} errors={s['errors']} "
        f"miss {s['missed_total']}/{s['expected_levels_total']} ({s['miss_rate']:.1%}) "
        f"extra {s['extra_total']}/{s['answered_levels_total']} ({s['extra_rate']:.1%}) "
        f"drift {s['drifted_total']}/{s['matched_total'] + s['drifted_total']} "
        f"({s['drift_rate']:.1%})"
    )
    if s["skipped"]:
        print(f"skipped: {s['skip_reasons']} (excluded from every rate)")
    if payload["errors"]:
        print(f"errors: {len(payload['errors'])} (recorded, resumed on next --resume)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_out() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return EVAL_DIR / stamp


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    ap.add_argument("--out", type=Path, default=None, help="artifact directory (UTC timestamp by default)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--resume", action="store_true", help="reuse the --out checkpoint and run missing items")
    args = ap.parse_args()

    from paknow.workflows.graph import run_query  # noqa: E402

    if args.resume and args.out is None:
        ap.error("--resume needs --out pointing at the interrupted run's directory")
    out_dir = args.out or _default_out()
    out_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = out_dir / "checkpoint.jsonl"

    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    items = list(dataset["items"])
    if args.limit:
        items = items[: args.limit]
    digest = file_sha256(args.dataset)
    checkpoint = load_checkpoint(checkpoint_path) if args.resume else {}
    todo = pending_items(items, checkpoint)

    started = time.monotonic()
    for index, item in enumerate(todo, 1):
        record = run_item(item, run_query)
        append_checkpoint(checkpoint_path, record)
        checkpoint[record_key(record)] = record
        print(f"[{index}/{len(todo)}] {item['id']} {record['status'].upper()} {record['elapsed_s']:.1f}s", flush=True)
    wall = time.monotonic() - started

    records = merge_records(items, checkpoint)
    payload = build_payload(
        dataset_path=args.dataset,
        digest=digest,
        records=records,
        limit=args.limit,
        timestamp=out_dir.name,
    )
    summary_path = out_dir / "summary.json"
    previous_wall = 0.0
    if summary_path.exists():
        try:
            previous_wall = float(json.loads(summary_path.read_text(encoding="utf-8")).get("wall_time_s") or 0)
        except (ValueError, json.JSONDecodeError):
            previous_wall = 0.0
    payload["wall_time_s"] = round(previous_wall + wall, 1)
    payload["ran"] = len(todo)
    write_jsonl(out_dir / "records.jsonl", records)
    summary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print_summary(payload)
    print(f"\nwrote {out_dir} (ran {len(todo)} of {len(items)})")


if __name__ == "__main__":
    main()
