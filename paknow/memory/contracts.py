"""Memory-layer domain contracts (plan §9, §12.1).

Five stores stay separate: source knowledge (evidence/graph), session state
(LangGraph checkpoint), user facts, episodic traces, procedural SOPs.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from paknow.evidence.contracts import utcnow


class FactScope(BaseModel):
    user_id: str = "default"
    scope: str = "global"            # global | market | symbol | timeframe | ...


class UserFactVersion(BaseModel):
    fact_id: str
    scope: FactScope = Field(default_factory=FactScope)
    key: str
    value: Any
    source: str                      # explicit user statement provenance
    valid_from: datetime = Field(default_factory=utcnow)
    valid_to: datetime | None = None
    recorded_at: datetime = Field(default_factory=utcnow)
    supersedes: str | None = None
    deleted: bool = False


class RunTrace(BaseModel):
    """Episodic record: request, route, tool params/results, evidence, output."""
    run_id: str
    request: dict[str, Any]
    intents: list[str] = Field(default_factory=list)
    routes: list[str] = Field(default_factory=list)
    version_snapshot: dict[str, str] = Field(default_factory=dict)  # component -> version
    evidence_ids: list[str] = Field(default_factory=list)
    output: dict[str, Any] | None = None
    # determinism / verification
    score_status: str = "unscored"
    failures: list[str] = Field(default_factory=list)
    elapsed_ms: int | None = None
    cost_usd: float | None = None
    created_at: datetime = Field(default_factory=utcnow)


class SOPStatus(str, Enum):
    observed = "observed"
    candidate = "candidate"
    replay_validated = "replay_validated"
    shadow = "shadow"
    active = "active"
    superseded = "superseded"
    retired = "retired"


class SOPStep(BaseModel):
    id: str
    operation: str
    depends_on: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)


class SOPValidation(BaseModel):
    dataset: str | None = None
    paired_runs: int = 0
    wins: int = 0
    losses: int = 0
    ties: int = 0
    approved: bool = False
    notes: str | None = None


class SOPVersion(BaseModel):
    """Procedural memory: validated workflow with applicability + stop rules."""
    sop_id: str
    version: int = 1
    status: SOPStatus = SOPStatus.candidate
    intent_scope: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    steps: list[SOPStep] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    fallback: str | None = None
    stop: str | None = None
    failure_patterns: list[str] = Field(default_factory=list)
    compatible_versions: dict[str, str] = Field(default_factory=dict)
    validation: SOPValidation = Field(default_factory=SOPValidation)
    supersedes: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
