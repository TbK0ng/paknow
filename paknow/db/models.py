"""SQLAlchemy ORM models mirroring the domain contracts (plan §12.1).

Nested objects (locator, qualifiers, steps, ...) are stored as JSON; the
fields used for lookup/filtering are extracted into indexed columns.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from paknow.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DocumentRow(Base):
    __tablename__ = "documents"

    doc_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    format: Mapped[str] = mapped_column(String(8))  # pdf | epub
    source_path: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    author: Mapped[str | None] = mapped_column(Text, nullable=True)
    translator: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    edition: Mapped[str | None] = mapped_column(Text, nullable=True)
    parser_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    entry_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvidenceUnitRow(Base):
    __tablename__ = "evidence_units"

    evidence_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    doc_id: Mapped[str] = mapped_column(ForeignKey("documents.doc_id"), index=True)
    locator: Mapped[dict[str, Any]] = mapped_column(JSON)
    text: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(String(64), index=True)
    block_type: Mapped[str] = mapped_column(String(16), default="paragraph")
    parent_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    child_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quality: Mapped[str] = mapped_column(String(16), default="native_text")
    adjacent_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    figure_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    parsed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # extracted query fields
    pdf_page_1based: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    printed_page_label: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    figure_label: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    epub_href: Mapped[str | None] = mapped_column(Text, nullable=True)
    chapter_path: Mapped[list[str]] = mapped_column(JSON, default=list)


class FigureAssetRow(Base):
    __tablename__ = "figure_assets"

    figure_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    doc_id: Mapped[str] = mapped_column(ForeignKey("documents.doc_id"), index=True)
    locator: Mapped[dict[str, Any]] = mapped_column(JSON)
    image_hash: Mapped[str] = mapped_column(String(64), index=True)
    rel_path: Mapped[str] = mapped_column(Text)
    width_px: Mapped[int] = mapped_column(Integer)
    height_px: Mapped[int] = mapped_column(Integer)
    page_bbox: Mapped[tuple | None] = mapped_column(JSON, nullable=True)
    caption_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    explanation_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    annotation_source: Mapped[str] = mapped_column(String(16), default="native")
    vlm_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_decorative: Mapped[bool] = mapped_column(Boolean, default=False)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    pdf_page_1based: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    figure_label: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)


class ImportJobRow(Base):
    __tablename__ = "import_jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    doc_id: Mapped[str] = mapped_column(ForeignKey("documents.doc_id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    scope: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class GraphNodeRow(Base):
    __tablename__ = "graph_nodes"

    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    node_type: Mapped[str] = mapped_column(String(32), index=True)
    label: Mapped[str] = mapped_column(Text)
    concept_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ConceptAliasRow(Base):
    __tablename__ = "concept_aliases"

    canonical_concept: Mapped[str] = mapped_column(String(64), primary_key=True)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    ambiguity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    context: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_status: Mapped[str] = mapped_column(String(16), default="pending")


class ClaimRow(Base):
    __tablename__ = "claims"

    claim_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject: Mapped[str] = mapped_column(Text, index=True)
    predicate: Mapped[str] = mapped_column(Text)
    polarity: Mapped[str] = mapped_column(String(16), default="positive")
    qualifiers: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    supporting_evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    contradicting_evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_scope: Mapped[str] = mapped_column(String(32), default="author_statement")
    empirical_validation: Mapped[str] = mapped_column(String(32), default="not_tested_here")
    extraction_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    extraction_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)


class GraphEdgeRow(Base):
    __tablename__ = "graph_edges"

    edge_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    from_node: Mapped[str] = mapped_column(String(64), index=True)
    to_node: Mapped[str] = mapped_column(String(64), index=True)
    relation: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(Text)
    extraction_status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserFactRow(Base):
    __tablename__ = "user_facts"

    fact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), default="default", index=True)
    scope: Mapped[str] = mapped_column(String(64), default="global")
    key: Mapped[str] = mapped_column(Text)
    value: Mapped[Any] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(Text)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    supersedes: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class RunTraceRow(Base):
    __tablename__ = "run_traces"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request: Mapped[dict[str, Any]] = mapped_column(JSON)
    intents: Mapped[list[str]] = mapped_column(JSON, default=list)
    routes: Mapped[list[str]] = mapped_column(JSON, default=list)
    version_snapshot: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    score_status: Mapped[str] = mapped_column(String(16), default="unscored")
    failures: Mapped[list[str]] = mapped_column(JSON, default=list)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class SOPRow(Base):
    __tablename__ = "sops"

    sop_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="candidate", index=True)
    intent_scope: Mapped[list[str]] = mapped_column(JSON, default=list)
    preconditions: Mapped[list[str]] = mapped_column(JSON, default=list)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    required_evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    fallback: Mapped[str | None] = mapped_column(Text, nullable=True)
    stop: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_patterns: Mapped[list[str]] = mapped_column(JSON, default=list)
    compatible_versions: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    validation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    supersedes: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# composite indexes for the hot lookup paths
Index("ix_evidence_doc_page", EvidenceUnitRow.doc_id, EvidenceUnitRow.pdf_page_1based)
Index("ix_evidence_doc_figure", EvidenceUnitRow.doc_id, EvidenceUnitRow.figure_label)
Index("ix_edges_from_rel", GraphEdgeRow.from_node, GraphEdgeRow.relation)
