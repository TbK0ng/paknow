"""Episodic run-trace persistence (plan §9.1)."""
from __future__ import annotations

from typing import Any

from paknow.db import SessionLocal
from paknow.db.models import RunTraceRow
from paknow.memory.versions import component_versions
from paknow.workflows.router import PlanSource


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
    sop_id: str | None = None,
    plan_source: PlanSource = "routed",
    query_hash: str = "",
) -> None:
    """Persist one run trace. ``plan_source`` is ``frozen`` for an injected
    query plan and ``routed`` when the router model produced it; ``query_hash``
    is the stable hash of the assembled query list."""
    request: dict[str, Any] = {
        "query": query,
        "plan_source": plan_source,
        "query_hash": query_hash,
    }
    if sop_id:
        request["sop"] = sop_id
    with SessionLocal() as s:
        s.add(
            RunTraceRow(
                run_id=run_id,
                request=request,
                intents=intents,
                routes=routes,
                evidence_ids=evidence_ids,
                output=answer,
                failures=failures,
                elapsed_ms=elapsed_ms,
                version_snapshot=component_versions(),
            )
        )
        s.commit()
