"""Local quality gate: did this change cost answer quality?

Two modes, both over a fixed 10-item stratified sample of the v2 gold dataset
(two or three items per stratum: encyclopedia / mechanism / exception /
conditioned claim) with the item's own plan injected, so routing never runs:

- ``--deterministic`` (default) drives the chain's own route/read/retrieve
  nodes and never calls a cloud model: hard constraints (a crashed retrieval,
  or an empty citable set that would force an empty answer) and
  expected-evidence coverage, compared against the committed baseline.
- ``--judge`` runs the same deterministic pass, then the full chain and the
  blind judge; it adds the answer-layer hard constraints (out-of-set citation,
  graph-only evidence cited, empty or degraded answer) and the judge floors.

Verdict: any hard-constraint failure is unconditional; soft metrics use the
measured noise bands — coverage may fall at most 0.03 (frozen queries are
deterministic), each judge dimension at most 0.12, the unsupported-claim total
may grow at most 15. The baseline carries the dataset hash and the sample, so
an edited dataset or a re-drawn sample refuses the old baseline; it is only
(re)written by ``--record``, and a failing run can never become the baseline.

Exit codes: 0 = pass, 1 = a regression, a hard failure, or an unusable
baseline. Run records (``*.records.jsonl`` + ``summary.json``) are written to
``--out`` per ADR-0004.

Usage:
    python scripts/quality_gate.py
    python scripts/quality_gate.py --deterministic   # the same, named explicitly
    python scripts/quality_gate.py --judge
    python scripts/quality_gate.py --record --out .scratch/production-readiness/quality-gate/baseline-<stamp>
    python scripts/quality_gate.py --record --judge   # records the judge block too
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.eval_answers import (  # noqa: E402
    ARMS,
    DEFAULT_DATASET,
    expected_coverage,
    frozen_plan,
    load_dataset,
    run_item,
)
from scripts.eval_common import (  # noqa: E402
    display_path,
    file_sha256,
    write_jsonl,
)
from scripts.eval_judge import (  # noqa: E402
    JUDGE_MAX_TOKENS,
    SCORE_KEYS,
    fetch_evidence_texts,
    judge_record,
    spread,
    summarize_arm as summarize_judged,
)

SAMPLE_SIZE = 10

#: measured noise bands (spec Further Notes): judge arm means move up to ±0.12
#: per dimension and the unsupported-claim total ±15 between two blind runs;
#: frozen-query coverage is deterministic, so its band is only 0.03
COVERAGE_FLOOR = 0.03
JUDGE_SCORE_FLOOR = 0.12
UNSUPPORTED_CLAIMS_FLOOR = 15

GATE_DIR = ROOT / ".scratch" / "production-readiness" / "quality-gate"
BASELINE = GATE_DIR / "baseline.json"


# ---------------------------------------------------------------------------
# sample
# ---------------------------------------------------------------------------


def stratified_sample(dataset: Any, limit: int = SAMPLE_SIZE) -> list[dict]:
    """``limit`` gold items across the strata, in dataset order.

    Apportioned by stratum size with the largest-remainder rule, then spread
    inside each stratum (both ends included), so the sample tracks the dataset
    and every stratum keeps its two or three items.
    """
    items = list(dataset["items"])
    strata: dict[str, list[dict]] = {}
    for item in items:
        strata.setdefault(str(item.get("category")), []).append(item)
    total = len(items)
    quota = {name: limit * len(group) // total for name, group in strata.items()}
    left = limit - sum(quota.values())
    order = list(strata)
    ranked = sorted(
        order,
        key=lambda name: (-(limit * len(strata[name]) % total), order.index(name)),
    )
    for name in ranked[:left]:
        quota[name] += 1
    picked = [item for name, group in strata.items() for item in spread(group, quota[name])]
    position = {str(item["id"]): index for index, item in enumerate(items)}
    return sorted(picked, key=lambda item: position[str(item["id"])])


# ---------------------------------------------------------------------------
# deterministic pass (no cloud call)
# ---------------------------------------------------------------------------


def run_deterministic(items: Sequence[dict]) -> list[dict]:
    """The chain's route/read/retrieve nodes over each item's frozen plan.

    The three model nodes (sufficiency, generation, and the router the plan
    replaces) are not reached, so this mode needs the index but works offline.
    Query assembly, SOP, retrieval and graph expansion stay the production
    code path rather than a second copy of it.
    """
    from paknow.workflows import graph as graph_module

    return [
        _retrieval_record(item, arm, expand_graph, graph_module)
        for item in items
        for arm, expand_graph in ARMS
    ]


def _retrieval_record(item: dict, arm: str, expand_graph: bool, graph_module) -> dict:
    expected = list(item.get("expected_evidence") or [])
    state: dict[str, Any] = {
        "query": item["question"],
        "images": [],
        "run_id": uuid.uuid4().hex,
        "t0": time.time(),
        "failures": [],
        "round": 0,
        "max_rounds": 3,
        "acc": {},
        "expand_graph": expand_graph,
        "plan": frozen_plan(item),
    }
    record: dict[str, Any] = {
        "id": item["id"],
        "arm": arm,
        "category": item.get("category"),
        "question": item["question"],
        "plan_source": "frozen",
        "queries": [],
        "query_hash": "",
        "citable_ids": [],
        "graph_evidence_ids": [],
        "expected_evidence": expected,
        "expected_covered": [],
        "expected_missing": expected,
        "coverage": 0.0,
        "coverage_complete": False,
        "failures": [],
        "status": "ok",
        "hard_pass": False,
        "hard_reasons": [],
        "elapsed_s": 0.0,
    }
    started = time.monotonic()
    try:
        state = graph_module._route_node(state)
        state = graph_module._read_node(state)
        state = graph_module._retrieve_node(state)
    except Exception as e:  # noqa: BLE001 - a crashed retrieval is the verdict
        record.update(
            status="error",
            error=f"{type(e).__name__}: {e}",
            hard_reasons=["retrieval_error"],
            elapsed_s=round(time.monotonic() - started, 3),
        )
        return record

    queries = list(state.get("queries") or [])
    citable = list(state.get("citation_index") or {})
    # no citable unit means generation could only produce an empty answer
    reasons = [] if citable else ["no_citable_evidence"]
    record.update(
        queries=queries,
        query_hash=graph_module.query_hash(queries),
        citable_ids=citable,
        graph_evidence_ids=[
            str(entry["evidence_id"])
            for entry in state.get("graph_evidence") or []
            if entry.get("evidence_id")
        ],
        failures=list(state.get("failures") or []),
        **expected_coverage(expected, citable),
        hard_pass=not reasons,
        hard_reasons=reasons,
        elapsed_s=round(time.monotonic() - started, 3),
    )
    return record


# ---------------------------------------------------------------------------
# judge pass (full chain + blind judge)
# ---------------------------------------------------------------------------


def run_judge(items: Sequence[dict]) -> list[dict]:
    """The full chain over the frozen plan, then one blind verdict per record.

    The verdict's own ``hard_verdict`` is the gate's hard constraint: the
    answer-layer rules (citation set, graph-only evidence, empty or degraded
    answer) are recomputed from the stored ids by ``eval_judge``.
    """
    from paknow.gateway import gateway
    from paknow.workflows.graph import run_query

    items_by_id = {str(item["id"]): item for item in items}
    records = [
        run_item(item, arm, run_query, frozen=True)
        for item in items
        for arm, _ in ARMS
    ]
    wanted = {
        str(evidence_id)
        for record in records
        for field in ("cited_evidence_ids", "expected_evidence")
        for evidence_id in record.get(field) or []
    }
    texts = fetch_evidence_texts(wanted)

    def ask(schema, messages):
        return gateway.chat_json(
            schema, messages, max_tokens=JUDGE_MAX_TOKENS, temperature=0.0
        )

    verdicts: list[dict] = []
    for record in records:
        verdict = judge_record(
            record, items_by_id[str(record["id"])], texts, ask
        )
        verdict["hard_pass"] = verdict["hard_verdict"] == "pass"
        verdicts.append(verdict)
    return verdicts


# ---------------------------------------------------------------------------
# blocks, floors, verdict
# ---------------------------------------------------------------------------


def _mean(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def measure_deterministic(records: Sequence[dict]) -> dict:
    """Per-arm hard-pass count and coverage: the deterministic baseline block."""
    arms: dict[str, dict] = {}
    for arm, _ in ARMS:
        group = [r for r in records if r["arm"] == arm]
        arms[arm] = {
            "items": len(group),
            "hard_pass": sum(1 for r in group if r.get("hard_pass")),
            "retrieval_errors": sum(1 for r in group if r.get("status") != "ok"),
            "coverage_mean": _mean([float(r.get("coverage") or 0) for r in group]),
        }
    return {"arms": arms}


def measure_judge(records: Sequence[dict]) -> dict:
    """Per-arm hard verdict and judge means: the blind-judge baseline block."""
    arms: dict[str, dict] = {}
    for arm, _ in ARMS:
        summary = summarize_judged([r for r in records if r["arm"] == arm])
        arms[arm] = {
            "items": summary["records"],
            "hard_pass": summary["hard_pass"],
            "hard_fail": summary["hard_fail"],
            "judged": summary["judged"],
            **{key: summary["scores"][key]["mean"] for key in SCORE_KEYS},
            "unsupported_claims_total": summary["unsupported_claims"]["total"],
        }
    return {"arms": arms}


def hard_failures(records: Sequence[dict]) -> list[str]:
    """Hard constraints are unconditional: no baseline can excuse one."""
    return [
        f"{r['id']} {r['arm']}: {','.join(r.get('hard_reasons') or ['hard_fail'])}"
        for r in records
        if not r.get("hard_pass")
    ]


def compare_block(mode: str, current: dict, baseline: dict) -> list[str]:
    """Every regression past its measured floor, one line each."""
    failures: list[str] = []
    for arm, now in current["arms"].items():
        was = (baseline.get("arms") or {}).get(arm)
        if not was:
            failures.append(f"{mode} {arm}: baseline records no {arm} arm")
            continue
        if now["hard_pass"] < was["hard_pass"]:
            failures.append(
                f"{mode} {arm}: hard_pass {now['hard_pass']} < baseline {was['hard_pass']}"
            )
        if mode == "deterministic":
            drop = float(was["coverage_mean"]) - float(now["coverage_mean"])
            if drop >= COVERAGE_FLOOR - 1e-9:
                failures.append(
                    f"{mode} {arm}: coverage_mean {now['coverage_mean']:.3f} < "
                    f"baseline {was['coverage_mean']:.3f} - {COVERAGE_FLOOR}"
                )
            continue
        for key in SCORE_KEYS:
            before, now_value = was.get(key), now.get(key)
            if before is None or now_value is None:
                failures.append(
                    f"{mode} {arm}: {key} not comparable (baseline {before}, now {now_value})"
                )
            elif float(before) - float(now_value) >= JUDGE_SCORE_FLOOR - 1e-9:
                failures.append(
                    f"{mode} {arm}: {key} {float(now_value):.2f} < "
                    f"baseline {float(before):.2f} - {JUDGE_SCORE_FLOOR}"
                )
        before_claims = int(was.get("unsupported_claims_total") or 0)
        growth = int(now["unsupported_claims_total"]) - before_claims
        if growth >= UNSUPPORTED_CLAIMS_FLOOR:
            failures.append(
                f"{mode} {arm}: unsupported_claims {now['unsupported_claims_total']} > "
                f"baseline {before_claims} + {UNSUPPORTED_CLAIMS_FLOOR}"
            )
    return failures


def print_block(mode: str, block: dict) -> None:
    for arm, stats in block["arms"].items():
        if mode == "deterministic":
            print(
                f"{mode:13} {arm:5} hard {stats['hard_pass']}/{stats['items']} "
                f"coverage {stats['coverage_mean']:.3f} "
                f"errors {stats['retrieval_errors']}"
            )
        else:
            means = " ".join(
                f"{key[:4]} {stats[key]:.2f}" if stats.get(key) is not None else f"{key[:4]} -"
                for key in SCORE_KEYS
            )
            print(
                f"{mode:13} {arm:5} hard {stats['hard_pass']}/{stats['items']} "
                f"{means} unsupported {stats['unsupported_claims_total']} "
                f"judged {stats['judged']}"
            )


# ---------------------------------------------------------------------------
# baseline
# ---------------------------------------------------------------------------


def load_baseline(
    path: Path, digest: str, sample_ids: Sequence[str], *, judge: bool
) -> tuple[dict | None, list[str]]:
    """The usable baseline, or why it cannot be compared."""
    if not path.exists():
        return None, [f"{display_path(path)}: no recorded baseline (record one with --record)"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("dataset_sha256") != digest:
        return None, [
            f"{display_path(path)}: dataset_sha256 {payload.get('dataset_sha256')} "
            f"!= {digest} (dataset changed; re-record with --record)"
        ]
    if list(payload.get("sample") or []) != list(sample_ids):
        return None, [
            f"{display_path(path)}: sample {len(payload.get('sample') or [])} items "
            f"!= current {len(sample_ids)} (re-record with --record)"
        ]
    if judge and "judge" not in payload:
        return None, [f"{display_path(path)}: no judge block (re-record with --record --judge)"]
    return payload, []


def record_payload(
    path: Path, digest: str, sample_ids: Sequence[str], dataset: Path, blocks: dict
) -> dict:
    """Merge the run's blocks into the file, keeping the other mode's block."""
    payload: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
        if existing.get("dataset_sha256") == digest:
            payload = existing
    payload.update(
        dataset=display_path(dataset),
        dataset_sha256=digest,
        recorded_at=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        sample=list(sample_ids),
    )
    payload.update(blocks)
    return payload


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_out() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return GATE_DIR / stamp


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--deterministic",
        action="store_true",
        help="deterministic checks only, no cloud call (default)",
    )
    mode.add_argument(
        "--judge", action="store_true", help="append the blind judge (needs the cloud key)"
    )
    ap.add_argument(
        "--record", action="store_true", help="write the baseline; a failing run is refused"
    )
    ap.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    ap.add_argument("--baseline", type=Path, default=BASELINE)
    ap.add_argument(
        "--out", type=Path, default=None, help="run record directory (UTC timestamp by default)"
    )
    args = ap.parse_args(argv)

    dataset = load_dataset(args.dataset)
    digest = file_sha256(args.dataset)
    items = stratified_sample(dataset)
    sample_ids = [str(item["id"]) for item in items]

    baseline: dict | None = None
    if not args.record:
        baseline, refusals = load_baseline(
            args.baseline, digest, sample_ids, judge=args.judge
        )
        if refusals:
            for line in refusals:
                print(f"refused {line}")
            return 1

    out_dir = args.out or _default_out()
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    records: dict[str, list[dict]] = {"deterministic": run_deterministic(items)}
    failures = hard_failures(records["deterministic"])
    if args.judge:
        records["judge"] = run_judge(items)
        failures += hard_failures(records["judge"])
    blocks = {"deterministic": measure_deterministic(records["deterministic"])}
    if args.judge:
        blocks["judge"] = measure_judge(records["judge"])
    wall = round(time.monotonic() - started, 1)

    for mode, block in blocks.items():
        print_block(mode, block)

    if args.record:
        if failures:
            for line in failures:
                print(f"FAIL {line}")
            print("refused to record: a failing run cannot become the baseline")
            return 1
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(
            json.dumps(
                record_payload(
                    args.baseline, digest, sample_ids, args.dataset, blocks
                ),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"recorded {display_path(args.baseline)}")
    else:
        for mode, block in blocks.items():
            failures += compare_block(mode, block, baseline[mode])

    for mode, rows in records.items():
        write_jsonl(out_dir / ("records.jsonl" if mode == "deterministic" else f"{mode}.records.jsonl"), rows)
    # the summary names the exact bytes it summarizes (ADR-0004)
    hashes = {"answers_sha256": file_sha256(out_dir / "records.jsonl")}
    if "judge" in records:
        hashes["judge_sha256"] = file_sha256(out_dir / "judge.records.jsonl")
    (out_dir / "summary.json").write_text(
        json.dumps(
            {
                "dataset": display_path(args.dataset),
                "dataset_sha256": digest,
                **hashes,
                "sample": sample_ids,
                "modes": list(blocks),
                "baseline": display_path(args.baseline),
                "recorded": bool(args.record),
                "wall_time_s": wall,
                "blocks": blocks,
                "failures": failures,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    for line in failures:
        print(f"FAIL {line}")
    print(f"wrote {display_path(out_dir)}")
    print("quality gate: pass" if not failures else f"quality gate: {len(failures)} regression(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
