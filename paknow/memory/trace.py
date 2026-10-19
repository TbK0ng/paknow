"""Episodic run-trace persistence (plan §9.1)."""
from __future__ import annotations

from typing import Any

from paknow.db import SessionLocal
from paknow.db.models import RunTraceRow


def save_trace(
    run_id: str,
    query: str,
    *,
    intents: list[str],
    routes: list[str],
    evidence_ids: list[str],
    answer: dict[str, Any] | None,
    failures: list[str],
    elapsed_ms: int | None,
) -> None:
    with SessionLocal() as s:
        s.add(
            RunTraceRow(
                run_id=run_id,
                request={"query": query},
                intents=intents,
                routes=routes,
                evidence_ids=evidence_ids,
                output=answer,
                failures=failures,
                elapsed_ms=elapsed_ms,
            )
        )
        s.commit()
