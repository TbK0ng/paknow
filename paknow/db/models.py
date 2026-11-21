"""Authoritative passage metadata and the reviewed PA graph."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from paknow.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DocumentRow(Base):
    __tablename__ = "documents"

    doc_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    format: Mapped[str] = mapped_column(String(8))
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
    provenance: Mapped[str] = mapped_column(String(16), default="native_text")
    adjacent_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    parsed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    pdf_page_1based: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    printed_page_label: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    figure_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    epub_href: Mapped[str | None] = mapped_column(Text, nullable=True)
    chapter_path: Mapped[list[str]] = mapped_column(JSON, default=list)


class GraphNodeRow(Base):
    __tablename__ = "graph_nodes"

    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    node_type: Mapped[str] = mapped_column(String(32), index=True)
    label: Mapped[str] = mapped_column(Text)
    concept_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    concept_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)


class ClaimRow(Base):
    __tablename__ = "claims"

    claim_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject: Mapped[str] = mapped_column(Text, index=True)
    subject_concept_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    predicate: Mapped[str] = mapped_column(Text)
    qualifiers: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    supporting_evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_scope: Mapped[str] = mapped_column(String(32))
    extraction_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)


class GraphEdgeRow(Base):
    __tablename__ = "graph_edges"

    edge_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    from_node: Mapped[str] = mapped_column(String(64), index=True)
    to_node: Mapped[str] = mapped_column(String(64), index=True)
    relation: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(String(16), index=True)
    extraction_status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


Index("ix_evidence_doc_page", EvidenceUnitRow.doc_id, EvidenceUnitRow.pdf_page_1based)
Index("ix_edges_from_rel", GraphEdgeRow.from_node, GraphEdgeRow.relation)
