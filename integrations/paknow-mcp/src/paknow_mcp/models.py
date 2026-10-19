"""Typed, wire-stable results for every PAknow MCP tool.

Models deliberately keep the backend's raw 0-based citation index and add a
1-based display index. Agents get both; humans get readable numbering.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Citation(BaseModel):
    index: int = Field(description="Raw 0-based index returned by PAknow")
    display_index: int = Field(description="1-based index for display and quoting")
    evidence_id: str
    citation: str = Field(description="Human-readable book/chapter/figure/page locator")
    text: str = Field(description="Original evidence text")
    doc_id: str | None = None


class FigureRef(BaseModel):
    figure_id: str
    figure_label: str | None = None
    rel_path: str | None = Field(
        default=None,
        description="Path under PAknow's data/objects; only meaningful on the PAknow host",
    )
    width_px: int | None = None
    height_px: int | None = None
    image_url: str | None = Field(
        default=None,
        description="Absolute /figures/{figure_id}/image URL on the PAknow service",
    )


class AskOutput(BaseModel):
    run_id: str
    intent: str | None = None
    is_framework: bool = False
    routes: list[str] = Field(default_factory=list)
    answer_raw: str = Field(description="PAknow answer with original 0-based [n] markers")
    answer_display: str = Field(description="Answer with 1-based [n] markers for humans")
    uncertainty: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    figures: list[FigureRef] = Field(default_factory=list)
    citation_index_base: Literal[0] = 0
    warnings: list[str] = Field(default_factory=list)


class SearchHit(BaseModel):
    evidence_id: str
    doc_id: str | None = None
    text: str
    score: float | None = None
    block_type: str | None = None
    chapter_path: list[str] = Field(default_factory=list)
    pdf_page: int | None = None
    figure_label: str | None = None
    epub_href: str | None = None
    figure_ids: list[str] = Field(default_factory=list)


class SearchOutput(BaseModel):
    query: str
    count: int
    hits: list[SearchHit] = Field(default_factory=list)


class EvidenceOutput(BaseModel):
    evidence_id: str
    text: str
    block_type: str | None = None
    chapter_path: list[str] = Field(default_factory=list)
    pdf_page: int | None = None
    printed_page: str | None = None
    figure_label: str | None = None
    epub_href: str | None = None
    doc_id: str | None = None
    doc_title: str | None = None
    figures: list[FigureRef] = Field(default_factory=list)


class DocumentInfo(BaseModel):
    doc_id: str
    title: str | None = None
    author: str | None = None
    translator: str | None = None
    language: str | None = None
    format: str | None = None
    edition: str | None = None
    page_count: int | None = None
    entry_count: int | None = None


class DocumentsOutput(BaseModel):
    count: int
    documents: list[DocumentInfo] = Field(default_factory=list)


class SourceOutput(DocumentInfo):
    evidence_units: int | None = None
    figures: int | None = None


class HealthOutput(BaseModel):
    status: str
    base_url: str
    latency_ms: int
    server_version: str | None = None


class RunOutput(BaseModel):
    run_id: str
    intents: list[str] = Field(default_factory=list)
    routes: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    output: Any = None
    failures: list[str] = Field(default_factory=list)
    elapsed_ms: int | None = None


class SOPInfo(BaseModel):
    sop_id: str
    version: int | None = None
    status: str | None = None
    intent_scope: Any = None
    steps: Any = None
    validation: Any = None


class SOPsOutput(BaseModel):
    count: int
    sops: list[SOPInfo] = Field(default_factory=list)


class ImageOutput(BaseModel):
    kind: Literal["page", "figure"]
    doc_id: str | None = None
    page: int | None = None
    scale: float | None = None
    figure_id: str | None = None
    figure_label: str | None = None
    mime_type: str
    byte_size: int
