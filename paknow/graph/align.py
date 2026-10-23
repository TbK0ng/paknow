"""Claim subject -> canonical concept alignment.

Deterministic first: an exact normalized alias resolves through
:func:`paknow.graph.identity.canonical_for`; otherwise dictionary matches on the
normalized subject align only when they point at exactly one canonical concept.
Multi-match and no-match subjects remain residual and may be offered to a
batched model, but only above a confidence threshold.

Alignment only fills ``claims.subject_concept_id``. It never overwrites an
existing id, never changes ``extraction_status`` (claims stay ``pending``), and
never touches evidence or ``SUPPORTED_BY`` edges, so aligned claims remain
non-citable auxiliary context (ADR-0002). Edges keep ``origin='claim_extract'``.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

from pydantic import BaseModel, Field

from paknow.config import settings
from paknow.db import SessionLocal
from paknow.db.models import ClaimRow, GraphNodeRow
from paknow.gateway import gateway
from paknow.graph.identity import (
    canonical_for,
    concept_kind_for,
    concept_node_id,
    label_for,
)
from paknow.retrieval.dictionary import find_terms, load_terms

METHOD_ALIAS = "alias"
METHOD_SUBSTRING = "substring"
METHOD_MODEL = "model"

DEFAULT_THRESHOLD = 0.8
DEFAULT_BATCH_SIZE = 25

_WS = re.compile(r"\s+")


class SubjectAlignment(BaseModel):
    """One model decision; ``index`` refers to the batch's subject order."""

    index: int
    concept_id: str = ""
    confidence: float = 0.0


class AlignmentBatch(BaseModel):
    alignments: list[SubjectAlignment] = Field(default_factory=list)


@dataclass(frozen=True)
class Alignment:
    concept_id: str
    method: str
    confidence: float | None = None


@dataclass
class AlignmentReport:
    total_claims: int
    originally_aligned: int
    newly_aligned: int
    residual: int
    by_method: dict[str, int] = field(default_factory=dict)
    residual_subjects: list[dict[str, Any]] = field(default_factory=list)
    applied: bool = False
    model_attempted: bool = False
    model_skipped: str | None = None
    model_errors: list[str] = field(default_factory=list)


def normalize_subject(text: str) -> str:
    """Collapse whitespace/NBSP; matching itself is case-insensitive."""
    return _WS.sub(" ", (text or "").replace("\xa0", " ")).strip()


def align_subject(subject: str) -> Alignment | None:
    """Deterministic alignment for one claim subject, or ``None`` when residual."""
    normalized = normalize_subject(subject)
    if not normalized:
        return None
    canonical = canonical_for(normalized)
    if canonical is not None:
        return Alignment(concept_id=canonical, method=METHOD_ALIAS)
    concepts = {m.canonical for m in find_terms(normalized)}
    if len(concepts) == 1:
        return Alignment(concept_id=next(iter(concepts)), method=METHOD_SUBSTRING)
    return None


def subject_candidates(subject: str) -> list[str]:
    """Every canonical concept the dictionary finds in the subject."""
    return sorted({m.canonical for m in find_terms(normalize_subject(subject))})


def _has_api_key() -> bool:
    return bool(settings.deepseek_api_key)


def _candidate_catalog() -> list[tuple[str, str]]:
    return sorted({(t.canonical, label_for(t.canonical)) for t in load_terms()})


def _messages(subjects: list[str], catalog: list[tuple[str, str]]) -> list[dict[str, str]]:
    candidates = "\n".join(f"- {canonical} | {label}" for canonical, label in catalog)
    numbered = "\n".join(f"{i}. {subject}" for i, subject in enumerate(subjects))
    return [
        {
            "role": "system",
            "content": (
                "你是价格行为概念对齐器。为每个主张主体选择最匹配的一个候选概念 id。"
                "只能从候选列表中选择；没有合适概念时 concept_id 留空。"
                "confidence 是 0 到 1 的校准置信度。只输出 JSON。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"候选概念（canonical | 中文标签）：\n{candidates}\n\n"
                f"主张主体（编号. 主体）：\n{numbered}\n\n"
                "返回 alignments，每项包含 index（主体编号）、concept_id、confidence。"
            ),
        },
    ]


def model_align_subjects(
    subjects: list[str],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> tuple[dict[str, Alignment], dict[str, float], list[str]]:
    """One model pass over residual subjects; fail-open per batch.

    Returns ``(accepted, best_confidence, errors)``. Accepted alignments are
    above ``threshold`` and use a candidate concept id; confidence is kept for
    rejected subjects so the residual report can explain why.
    """
    catalog = _candidate_catalog()
    allowed = {canonical for canonical, _label in catalog}
    accepted: dict[str, Alignment] = {}
    confidence: dict[str, float] = {}
    errors: list[str] = []

    for offset in range(0, len(subjects), batch_size):
        batch = subjects[offset : offset + batch_size]
        try:
            result = gateway.chat_json(
                AlignmentBatch,
                _messages(batch, catalog),
                max_tokens=4096,
                temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001 - fail-open by design
            errors.append(f"batch@{offset}: {type(exc).__name__}: {exc}")
            continue

        for item in getattr(result, "alignments", []) or []:
            if not (0 <= item.index < len(batch)):
                continue
            subject = batch[item.index]
            score = float(item.confidence)
            confidence[subject] = max(confidence.get(subject, 0.0), score)
            concept = (item.concept_id or "").strip()
            if concept not in allowed or score < threshold:
                continue
            accepted[subject] = Alignment(
                concept_id=concept, method=METHOD_MODEL, confidence=score
            )
    return accepted, confidence, errors


def _residual_entries(
    subjects: dict[str, list[str]],
    remaining: Iterable[str],
    confidences: dict[str, float],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for subject in remaining:
        candidates = subject_candidates(subject)
        entry: dict[str, Any] = {
            "subject": subject,
            "claims": len(subjects[subject]),
            "reason": "multi_match" if len(candidates) > 1 else "no_match",
            "candidates": candidates,
        }
        if subject in confidences:
            entry["model_confidence"] = round(confidences[subject], 3)
        entries.append(entry)
    entries.sort(key=lambda e: (-e["claims"], e["subject"]))
    return entries


def align_claims(
    *,
    apply: bool = False,
    use_model: bool = False,
    threshold: float = DEFAULT_THRESHOLD,
    batch_size: int = DEFAULT_BATCH_SIZE,
    session_factory=None,
) -> AlignmentReport:
    """Plan (and optionally write) claim -> concept alignment.

    ``apply=False`` is a dry run: counts and residual subjects are reported but
    nothing is written.
    """
    factory = session_factory or SessionLocal

    with factory() as s:
        total = s.query(ClaimRow).count()
        before = s.query(ClaimRow).filter(ClaimRow.subject_concept_id.isnot(None)).count()
        rows = s.query(ClaimRow).filter(ClaimRow.subject_concept_id.is_(None)).all()
        subjects: dict[str, list[str]] = {}
        for row in rows:
            subjects.setdefault(normalize_subject(row.subject), []).append(row.claim_id)

        plan: dict[str, Alignment] = {}
        for subject in subjects:
            alignment = align_subject(subject)
            if alignment is not None:
                plan[subject] = alignment

        model_attempted = False
        model_skipped: str | None = None
        model_errors: list[str] = []
        confidences: dict[str, float] = {}

        if use_model:
            pending = [subject for subject in subjects if subject not in plan]
            if not pending:
                model_skipped = "no_residual"
            elif not _has_api_key():
                model_skipped = "no_api_key"
            else:
                model_attempted = True
                accepted, confidences, model_errors = model_align_subjects(
                    pending, threshold=threshold, batch_size=batch_size
                )
                plan.update(accepted)

        planned_by_method = Counter()
        for subject, alignment in plan.items():
            planned_by_method[alignment.method] += len(subjects[subject])
        planned = sum(planned_by_method.values())

        remaining = [subject for subject in subjects if subject not in plan]
        residual_entries = _residual_entries(subjects, remaining, confidences)

        written = 0
        if apply and plan:
            for subject, alignment in plan.items():
                written += (
                    s.query(ClaimRow)
                    .filter(
                        ClaimRow.subject == subject,
                        ClaimRow.subject_concept_id.is_(None),
                    )
                    .update(
                        {ClaimRow.subject_concept_id: alignment.concept_id},
                        synchronize_session=False,
                    )
                )
            existing = {
                node.concept_id
                for node in s.query(GraphNodeRow)
                .filter(GraphNodeRow.node_type == "concept")
                .all()
            }
            for canonical in sorted({a.concept_id for a in plan.values()} - existing):
                s.add(
                    GraphNodeRow(
                        node_id=concept_node_id(canonical),
                        node_type="concept",
                        label=label_for(canonical),
                        concept_id=canonical,
                        concept_kind=concept_kind_for(canonical),
                    )
                )
            s.commit()

        newly_aligned = written if apply else planned
        return AlignmentReport(
            total_claims=total,
            originally_aligned=before,
            newly_aligned=newly_aligned,
            residual=total - before - newly_aligned,
            by_method=dict(planned_by_method),
            residual_subjects=residual_entries,
            applied=apply,
            model_attempted=model_attempted,
            model_skipped=model_skipped,
            model_errors=model_errors,
        )
