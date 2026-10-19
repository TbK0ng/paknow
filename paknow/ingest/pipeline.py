"""Ingest orchestration: register -> parse -> persist -> update metadata."""
from __future__ import annotations

from paknow.db import SessionLocal
from paknow.db.models import DocumentRow
from paknow.ingest.chunk import chunk_units
from paknow.ingest.epub import EpubParseResult, parse_epub
from paknow.ingest.manifest import register_document
from paknow.ingest.pdf import PdfParseResult, parse_pdf
from paknow.ingest.persist import persist_figures, persist_units


def ingest_epub(path: str) -> tuple[DocumentRow, EpubParseResult]:
    row = register_document(path)
    result = parse_epub(path, row.doc_id)
    result.evidence_units = chunk_units(result.evidence_units)
    persist_units(result.evidence_units)
    persist_figures(result.figures)
    with SessionLocal() as s:
        r = s.get(DocumentRow, row.doc_id)
        assert r is not None
        r.title = result.title
        r.author = result.author
        r.language = result.language
        r.entry_count = result.entry_count
        s.commit()
    return row, result


def ingest_pdf(
    path: str, *, page_range: tuple[int, int] | None = None
) -> tuple[DocumentRow, PdfParseResult]:
    row = register_document(path)
    result = parse_pdf(path, row.doc_id, page_range=page_range)
    result.evidence_units = chunk_units(result.evidence_units)
    persist_units(result.evidence_units)
    persist_figures(result.figures)
    with SessionLocal() as s:
        r = s.get(DocumentRow, row.doc_id)
        assert r is not None
        r.page_count = result.page_count
        s.commit()
    return row, result
