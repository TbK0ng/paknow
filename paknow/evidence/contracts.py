"""Evidence-layer domain contracts (plan §12.1).

These are the authoritative in-memory domain types. PostgreSQL tables
mirror them; indexes (Qdrant) only carry vectors + a pointer back to an
``evidence_id``/``figure_id``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# enums
# --------------------------------------------------------------------------- #
class DocFormat(str, Enum):
    pdf = "pdf"
    epub = "epub"


class Provenance(str, Enum):
    """How an evidence unit's text came to exist (not how trustworthy it is).

    ``derived_observation`` is index material only: it never enters the
    citable evidence set (ADR-0002).
    """
    native_text = "native_text"              # text layer extracted directly
    ocr = "ocr"                              # produced by OCR fallback
    derived_observation = "derived_observation"  # VLM reading of a page/figure


class ReviewStatus(str, Enum):
    pending = "pending"
    reviewed = "reviewed"
    rejected = "rejected"


# --------------------------------------------------------------------------- #
# locator: reversible source position across PDF/EPUB
# --------------------------------------------------------------------------- #
class SourceLocator(BaseModel):
    """Unambiguous position in a source document.

    PDF keeps physical page (1-based), printed label (may repeat/miss),
    chapter path and figure label separately. EPUB positions by spine order
    href + fragment id; it has no page number.
    """
    doc_id: str
    format: DocFormat
    # PDF only
    pdf_page_1based: int | None = None
    printed_page_label: str | None = None
    figure_label: str | None = None
    # EPUB only
    epub_href: str | None = None
    epub_fragment: str | None = None
    # both
    chapter_path: list[str] = Field(default_factory=list)
    # region in *rendered page* points (x0, y0, x1, y1); None = whole page
    bbox: tuple[float, float, float, float] | None = None
    rotation: int = 0
    render_scale: float = 1.0


# --------------------------------------------------------------------------- #
# documents
# --------------------------------------------------------------------------- #
class DocumentVersion(BaseModel):
    doc_id: str
    content_hash: str
    format: DocFormat
    source_path: str              # read-only original path
    title: str | None = None
    author: str | None = None
    translator: str | None = None
    language: str | None = None
    edition: str | None = None
    parser_version: str | None = None
    # structure stats measured at ingest time
    page_count: int | None = None
    entry_count: int | None = None
    registered_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# evidence units
# --------------------------------------------------------------------------- #
class EvidenceUnit(BaseModel):
    evidence_id: str
    doc_id: str
    locator: SourceLocator
    text: str                      # raw text of the unit (semantic content)
    text_hash: str
    block_type: Literal["paragraph", "heading", "caption", "figure_ref", "table", "footer", "other"] = "paragraph"
    # parent/child chunking links
    parent_id: str | None = None
    child_ids: list[str] = Field(default_factory=list)
    # token estimate (recorded tokenizer-dependent, updated on chunking)
    token_count: int | None = None
    provenance: Provenance = Provenance.native_text
    # neighboring cross-page / cross-region links
    adjacent_ids: list[str] = Field(default_factory=list)
    figure_ids: list[str] = Field(default_factory=list)
    parsed_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# figure assets
# --------------------------------------------------------------------------- #
class FigureAsset(BaseModel):
    figure_id: str
    doc_id: str
    locator: SourceLocator
    image_hash: str                 # hash of the original image bytes
    rel_path: str                   # path under data/objects
    width_px: int
    height_px: int
    # region transform relative to its parent page (reversible)
    page_bbox: tuple[float, float, float, float] | None = None
    caption_ids: list[str] = Field(default_factory=list)   # EvidenceUnit ids
    explanation_ids: list[str] = Field(default_factory=list)
    annotation_source: Literal["native", "layout_model", "manual"] = "native"
    # derived description is a *derived index*, never the authoritative source
    vlm_description: str | None = None
    description_model: str | None = None
    is_decorative: bool = False
    registered_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# import job: page/section-level worklist with retry (plan §5.1)
# --------------------------------------------------------------------------- #
class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


class ImportJob(BaseModel):
    job_id: str
    doc_id: str
    kind: str                       # "pdf_parse", "epub_parse", "index", ...
    scope: str                      # page range / section key
    status: JobStatus = JobStatus.queued
    attempt: int = 0
    error: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
