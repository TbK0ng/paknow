"""Text evidence and its reversible source location."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class DocFormat(str, Enum):
    pdf = "pdf"
    epub = "epub"


class Provenance(str, Enum):
    native_text = "native_text"
    ocr = "ocr"


class SourceLocator(BaseModel):
    """PDF physical/printed page and bounds, or EPUB spine item and fragment."""

    doc_id: str
    format: DocFormat
    pdf_page_1based: int | None = None
    printed_page_label: str | None = None
    figure_label: str | None = None  # author-supplied text, not an image ID
    epub_href: str | None = None
    epub_fragment: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    context_only: bool = False
    span_start: int | None = None
    span_end: int | None = None
    chapter_path: list[str] = Field(default_factory=list)
    bbox: tuple[float, float, float, float] | None = None


class EvidenceUnit(BaseModel):
    evidence_id: str
    doc_id: str
    locator: SourceLocator
    text: str
    text_hash: str
    block_type: Literal["paragraph", "heading", "caption", "table", "footer", "other"] = "paragraph"
    parent_id: str | None = None
    child_ids: list[str] = Field(default_factory=list)
    token_count: int | None = None
    provenance: Provenance = Provenance.native_text
    adjacent_ids: list[str] = Field(default_factory=list)
    parsed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
