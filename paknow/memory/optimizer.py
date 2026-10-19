"""Automatic 评价→提取改进→发布 loop (plan §9.4), memory versioned via git.

- 评价: programmatic signals from episodic traces (retrieval error / no
  evidence / low evidence / no answer), grouped by intent.
- 提取: LLM distills a candidate retrieval SOP from recurring failures.
- 发布: candidate SOP is persisted + git-committed (versioned); a candidate
  auto-promotes to ``shadow`` when a failure recurs strongly, ``active`` still
  requires replay validation (plan §9.4 gate).
"""
from __future__ import annotations

import threading
from collections import Counter
from typing import Any

from pydantic import BaseModel

from paknow.db import SessionLocal
from paknow.db.models import RunTraceRow
from paknow.gateway import gateway
from paknow.memory import sop as sop_registry
from paknow.memory.contracts import SOPStatus
from paknow.memory.versions import component_versions

AUTO_THRESHOLD = 20  # new unscored traces before auto-optimization


def evaluate_run(output: Any, failures: Any) -> list[str]:
    problems: list[str] = []
    if failures:
        problems.append("retrieval_error")
    out = output or {}
    ev = out.get("evidence_ids") or []
    if not ev:
        problems.append("no_evidence")
    elif len(ev) < 2:
        problems.append("low_evidence")
    if not out.get("answer"):
        problems.append("no_answer")
    return problems


def evaluate(limit: int = 300) -> tuple[dict[str, Counter], list[RunTraceRow]]:
    """Group problem tags by intent from recent unscored traces."""
    with SessionLocal() as s:
        traces = (
            s.query(RunTraceRow)
            .filter(RunTraceRow.score_status == "unscored")
            .order_by(RunTraceRow.created_at.desc())
            .limit(limit)
            .all()
        )
    by_intent: dict[str, Counter] = {}
    for t in traces:
        intent = (t.intents or ["unknown"])[0]
        for p in evaluate_run(t.output, t.failures or []):
            by_intent.setdefault(intent, Counter())[p] += 1
    return by_intent, traces


class CandidateSOP(BaseModel):
    sop_id: str
    intent_scope: list[str]
    preconditions: list[str]
    steps: list[dict[str, Any]]
    required_evidence: list[str]
    fallback: str
    stop: str
    failure_patterns: list[str]
    rationale: str


_DISTILL_SYSTEM = """你是检索系统的记忆优化器。根据最近回答质量有问题的记录（意图+失败类型计数+问题样本），提炼一条候选"检索 SOP"来减少这类失败。
- sop_id: 短横线命名（如 retrieval_fix_no_evidence）。
- steps: 有序步骤，operation 用可执行动作（expand_terms / step_back / multi_query / hyde / hybrid_search / expand_figures / check_evidence / rerank ...）。
- preconditions: 适用条件；failure_patterns: 这条 SOP 要覆盖/避免的失败模式。
- required_evidence: 必须凑齐的证据类型；fallback: 证据不足退路；stop: 停止条件。
- rationale: 一句话说明为何能改善。
只依据给定失败记录提炼，不凭空编造。"""


def distill(intent: str, problems: Counter, samples: list[str]) -> CandidateSOP:
    user = (
        f"意图: {intent}\n失败类型计数: {dict(problems)}\n"
        f"最近相关问题样本:\n" + "\n".join(f"- {q}" for q in samples[:8])
    )
    msgs = [
        {"role": "system", "content": _DISTILL_SYSTEM},
        {"role": "user", "content": user},
    ]
    try:
        return gateway.chat_json(CandidateSOP, msgs, max_tokens=1500)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"distill failed: {e}") from e


def run_loop(threshold: int = 3, limit: int = 300) -> list[dict[str, Any]]:
    """评价→蒸馏→发布。返回本轮发布的候选 SOP 摘要。"""
    by_intent, traces = evaluate(limit)
    samples_by_intent: dict[str, list[str]] = {}
    for t in traces:
        intent = (t.intents or ["unknown"])[0]
        q = (t.request or {}).get("query", "")
        if q:
            samples_by_intent.setdefault(intent, []).append(q)

    published: list[dict[str, Any]] = []
    for intent, problems in by_intent.items():
        total = sum(problems.values())
        if total < threshold:
            continue
        try:
            cand = distill(intent, problems, samples_by_intent.get(intent, []))
            status = SOPStatus.shadow if total >= threshold * 2 else SOPStatus.candidate
            sop_registry.create_sop(
                cand.sop_id,
                intent_scope=cand.intent_scope,
                preconditions=cand.preconditions,
                steps=cand.steps,
                required_evidence=cand.required_evidence,
                fallback=cand.fallback,
                stop=cand.stop,
                failure_patterns=cand.failure_patterns,
                compatible_versions=component_versions(),
            )
            if status == SOPStatus.shadow:
                sop_registry.promote(cand.sop_id, SOPStatus.shadow)
            published.append(
                {
                    "sop_id": cand.sop_id,
                    "intent": intent,
                    "problems": dict(problems),
                    "status": status.value,
                    "rationale": cand.rationale,
                }
            )
        except Exception as e:  # noqa: BLE001  one intent failing must not kill the loop
            published.append({"intent": intent, "error": str(e)})

    # mark processed traces so they are not re-evaluated next time
    ids = [t.run_id for t in traces]
    if ids:
        with SessionLocal() as s:
            s.query(RunTraceRow).filter(RunTraceRow.run_id.in_(ids)).update(
                {"score_status": "optimized"}, synchronize_session=False
            )
            s.commit()
    return published


def should_optimize(threshold: int = AUTO_THRESHOLD) -> bool:
    with SessionLocal() as s:
        n = s.query(RunTraceRow).filter(RunTraceRow.score_status == "unscored").count()
    return n >= threshold


_lock = threading.Lock()


def auto_optimize() -> bool:
    """Non-blocking auto-trigger; returns True if a loop actually ran."""
    if not should_optimize():
        return False
    if not _lock.acquire(blocking=False):
        return False
    try:
        run_loop()
        return True
    finally:
        _lock.release()
