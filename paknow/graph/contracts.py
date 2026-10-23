"""Graph-layer domain contracts (plan §6, §12.1).

Three graph semantics stay distinct; here we model the *knowledge/evidence
graph* (concepts, conditions, claims, sources). The document structure graph
is derivable from ``EvidenceUnit``/``FigureAsset`` links; the SOP task graph
lives in ``paknow.memory``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from paknow.evidence.contracts import ReviewStatus


class NodeType(str, Enum):
    """Entities that live in ``graph_nodes``.

    Claims, evidence units, documents, sections and bar references keep their
    own tables; ``GraphEdge`` endpoints may point at them (see below).
    """
    concept = "concept"
    figure = "figure"


class ConceptKind(str, Enum):
    """What kind of concept a node is; an attribute, not a node type."""
    term = "term"          # 通用术语（趋势、价格）
    pattern = "pattern"    # 可识别形态（双底多头旗形）
    regime = "regime"      # 市场环境（强趋势、交易区间日）
    setup = "setup"        # 具体入场结构（高二、突破回调）


class Relation(str, Enum):
    DEFINED_BY = "DEFINED_BY"
    REQUIRES_CONTEXT = "REQUIRES_CONTEXT"
    ILLUSTRATED_BY = "ILLUSTRATED_BY"
    SUPPORTED_BY = "SUPPORTED_BY"
    CONTRADICTED_BY = "CONTRADICTED_BY"
    INVALIDATED_BY = "INVALIDATED_BY"
    EXCEPTION_TO = "EXCEPTION_TO"
    ALIAS_OF = "ALIAS_OF"
    PART_OF = "PART_OF"
    TRANSITIONS_TO = "TRANSITIONS_TO"


class GraphNode(BaseModel):
    node_id: str
    node_type: NodeType
    label: str
    # canonical concept id when node_type is concept
    concept_id: str | None = None
    concept_kind: ConceptKind | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class ConceptAlias(BaseModel):
    """人工校验词典 entry (plan §5.5)."""
    canonical_concept: str           # 规范概念 id, e.g. "double_bottom_bull_flag"
    aliases: list[str] = Field(default_factory=list)  # zh + en + abbreviations
    ambiguity_type: str | None = None  # e.g. BTC = Buy The Close vs Bitcoin
    context: str | None = None       # disambiguation context
    review_status: ReviewStatus = ReviewStatus.pending


class Claim(BaseModel):
    """带条件的记录：不是 "高二 -> 买入"，而是 背景+触发+失效 下的主张."""
    claim_id: str
    subject: str                     # surface phrasing as extracted
    subject_concept_id: str | None = None  # canonical concept when the subject resolves
    predicate: str
    polarity: str = "positive"       # positive | negative | neutral
    qualifiers: dict[str, Any] = Field(default_factory=dict)  # context, modality, ...
    supporting_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)
    source_scope: str = "author_statement"  # author_statement | operational_def | ...
    empirical_validation: str = "not_tested_here"
    extraction_status: ReviewStatus = ReviewStatus.pending
    extraction_confidence: float | None = None  # model confidence, NOT empirical prob


class GraphEdge(BaseModel):
    """Directed semantic edge between entity references.

    ``from_node`` and ``to_node`` hold one of: a ``graph_nodes.node_id``
    (concept or figure), a ``claim_id``, or an ``evidence_id``. Claim and
    evidence endpoints are legitimate and have no node row — resolve them via
    their own tables.
    """
    edge_id: str
    from_node: str
    to_node: str
    relation: Relation
    source: str                      # evidence_id / doc_id proving the relation
    # how the edge came to exist; keeps rebuilds from touching manual seeds
    origin: str = "claim_extract"    # heading_parse | claim_extract | figure_extract | seed
    extraction_status: ReviewStatus = ReviewStatus.pending
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
