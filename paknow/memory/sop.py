"""SOP (procedural memory) registry: lifecycle + persistence (plan §9.3, §9.4).

States: observed -> candidate -> replay_validated -> shadow -> active ->
superseded/retired. Promotion to ``active`` requires an offline replay against
a validation dataset; without one the SOP stays ``candidate``.
"""
from __future__ import annotations

from typing import Any

from paknow.db import SessionLocal
from paknow.db.models import SOPRow
from paknow.memory import gitstore
from paknow.memory.contracts import SOPStatus, SOPValidation


def _snapshot(row: SOPRow, action: str) -> None:
    """Mirror the SOP to git and commit (memory version management)."""
    gitstore.write_sop(
        {
            "sop_id": row.sop_id,
            "version": row.version,
            "status": row.status,
            "intent_scope": row.intent_scope,
            "preconditions": row.preconditions,
            "steps": row.steps,
            "required_evidence": row.required_evidence,
            "fallback": row.fallback,
            "stop": row.stop,
            "failure_patterns": row.failure_patterns,
            "compatible_versions": row.compatible_versions,
            "validation": row.validation,
        }
    )
    gitstore.commit(f"{action}: {row.sop_id} v{row.version} [{row.status}]")


def create_sop(
    sop_id: str,
    *,
    intent_scope: list[str],
    preconditions: list[str],
    steps: list[dict[str, Any]],
    required_evidence: list[str],
    fallback: str | None = None,
    stop: str | None = None,
    failure_patterns: list[str] | None = None,
    compatible_versions: dict[str, str] | None = None,
) -> SOPRow:
    with SessionLocal() as s:
        row = SOPRow(
            sop_id=sop_id,
            version=1,
            status=SOPStatus.candidate.value,
            intent_scope=intent_scope,
            preconditions=preconditions,
            steps=steps,
            required_evidence=required_evidence,
            fallback=fallback,
            stop=stop,
            failure_patterns=failure_patterns or [],
            compatible_versions=compatible_versions or {},
            validation=SOPValidation().model_dump(),
        )
        s.add(row)
        s.commit()
        _snapshot(row, "create")
        return row


def record_replay(sop_id: str, *, wins: int, losses: int, ties: int, dataset: str) -> None:
    with SessionLocal() as s:
        row = s.get(SOPRow, sop_id)
        if row is None:
            return
        val = dict(row.validation or {})
        val.update(
            {
                "dataset": dataset,
                "paired_runs": wins + losses + ties,
                "wins": wins,
                "losses": losses,
                "ties": ties,
            }
        )
        row.validation = val
        if wins > losses and wins >= 3:
            row.status = SOPStatus.replay_validated.value
        s.commit()
        _snapshot(row, "replay")


def promote(sop_id: str, status: SOPStatus) -> None:
    with SessionLocal() as s:
        row = s.get(SOPRow, sop_id)
        if row is None:
            return
        row.status = status.value
        s.commit()
        _snapshot(row, "promote")


def list_sops(status: str | None = None) -> list[dict[str, Any]]:
    with SessionLocal() as s:
        q = s.query(SOPRow)
        if status:
            q = q.filter(SOPRow.status == status)
        return [
            {
                "sop_id": r.sop_id,
                "version": r.version,
                "status": r.status,
                "intent_scope": r.intent_scope,
                "steps": r.steps,
                "validation": r.validation,
            }
            for r in q.all()
        ]


_STATUS_RANK = {"active": 3, "shadow": 2, "candidate": 1}


def select_sop(intent: str) -> dict[str, Any] | None:
    """Best applicable SOP for an intent: active > shadow > candidate.

    Skips SOPs whose ``compatible_versions`` no longer match the current
    corpus/model fingerprint (plan §9.6 stale-memory guard).
    """
    from paknow.memory.versions import component_versions

    current = component_versions()
    best: SOPRow | None = None
    with SessionLocal() as s:
        for r in s.query(SOPRow).all():
            if r.status not in _STATUS_RANK:
                continue
            scope = [x.lower() for x in (r.intent_scope or [])]
            if intent.lower() not in scope:
                continue
            if r.compatible_versions:
                if any(current.get(k) != v for k, v in r.compatible_versions.items()):
                    continue
            if best is None or (
                _STATUS_RANK[r.status],
                r.version,
            ) > (_STATUS_RANK[best.status], best.version):
                best = r
    if best is None:
        return None
    return {
        "sop_id": best.sop_id,
        "status": best.status,
        "version": best.version,
        "intent_scope": best.intent_scope or [],
        "preconditions": best.preconditions or [],
        "steps": best.steps or [],
        "required_evidence": best.required_evidence or [],
        "fallback": best.fallback,
        "stop": best.stop,
        "failure_patterns": best.failure_patterns or [],
        "compatible_versions": best.compatible_versions or {},
    }
