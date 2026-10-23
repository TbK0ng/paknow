"""Align claim subjects to canonical concepts; report original / new / residual.

Deterministic alias + single-concept matching always runs. ``--model`` adds a
batched model pass for the residual, gated on a confidence threshold and an API
key; failures leave the residual unchanged. Dry run by default.

Writes the residual subject list to ``.scratch/graph-expansion/claim-residuals.json``
and a summary to ``.scratch/graph-expansion/claim-alignment.md``. Idempotent:
a second ``--apply`` writes zero new alignments.

Example:
    PAKNOW_DB_URL=sqlite:///data/paknow.db python scripts/align_claims.py
    PAKNOW_DB_URL=sqlite:///data/paknow.db python scripts/align_claims.py --apply
    PAKNOW_DB_URL=sqlite:///data/paknow.db python scripts/align_claims.py --apply --model
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from paknow.config import settings
from paknow.graph.align import DEFAULT_BATCH_SIZE, DEFAULT_THRESHOLD, AlignmentReport, align_claims

_REPORT_DIR = Path(".scratch") / "graph-expansion"


def _report_dir() -> Path:
    root = Path(settings.project_root)
    out = root / _REPORT_DIR
    out.mkdir(parents=True, exist_ok=True)
    return out


def _write_reports(report: AlignmentReport, *, args: argparse.Namespace) -> tuple[Path, Path]:
    out = _report_dir()
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")

    residuals = out / "claim-residuals.json"
    residuals.write_text(
        json.dumps(report.residual_subjects, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    mode = "apply" if args.apply else "dry-run"
    model = "on" if args.model else "off"
    if report.model_skipped:
        model += f" (skipped: {report.model_skipped})"
    methods = ", ".join(f"{k}={v}" for k, v in sorted(report.by_method.items())) or "none"
    errors = "\n".join(f"- {e}" for e in report.model_errors) or "- none"
    summary = out / "claim-alignment.md"
    summary.write_text(
        "\n".join(
            [
                "# Claim → concept alignment",
                "",
                f"- database: `{settings.paknow_db_url}`",
                f"- mode: {mode}",
                f"- model: {model}",
                f"- threshold: {args.threshold}",
                f"- generated: {generated}",
                "",
                "| metric | claims |",
                "| --- | ---: |",
                f"| originally aligned | {report.originally_aligned} |",
                f"| newly aligned by this run | {report.newly_aligned} |",
                f"| residual | {report.residual} |",
                "",
                f"Total claims: {report.total_claims}. New alignments by method: {methods}.",
                "",
                f"Residual subjects: {len(report.residual_subjects)} distinct "
                "(see `claim-residuals.json`).",
                "",
                "Model errors:",
                errors,
                "",
                "Claims stay `extraction_status='pending'` and are not citable evidence; "
                "their `SUPPORTED_BY` edges keep `origin='claim_extract'`.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return residuals, summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write alignments (default: dry run)")
    ap.add_argument("--model", action="store_true", help="run the batch model pass on residuals")
    ap.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"minimum model confidence to write (default {DEFAULT_THRESHOLD})",
    )
    ap.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"subjects per model call (default {DEFAULT_BATCH_SIZE})",
    )
    args = ap.parse_args()

    report = align_claims(
        apply=args.apply,
        use_model=args.model,
        threshold=args.threshold,
        batch_size=args.batch_size,
    )
    residuals, summary = _write_reports(report, args=args)

    if report.model_skipped:
        print(f"model skipped: {report.model_skipped}", file=sys.stderr)
    for error in report.model_errors:
        print(f"model error: {error}", file=sys.stderr)
    print(f"reports: {residuals} , {summary}", file=sys.stderr)

    print(f"originally_aligned: {report.originally_aligned}")
    print(f"newly_aligned: {report.newly_aligned}")
    print(f"residual: {report.residual}")


if __name__ == "__main__":
    main()
