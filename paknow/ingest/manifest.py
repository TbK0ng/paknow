"""Document registration and version manifest (plan §5.1)."""
from __future__ import annotations

from pathlib import Path

from paknow.db import SessionLocal
from paknow.db.models import DocumentRow
from paknow.evidence.contracts import DocFormat
from paknow.ids import file_hash, stable_id


def detect_format(path: str) -> DocFormat:
    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        return DocFormat.pdf
    if ext == ".epub":
        return DocFormat.epub
    raise ValueError(f"unsupported source format: {ext!r}")


def register_document(
    path: str,
    *,
    title: str | None = None,
    author: str | None = None,
    translator: str | None = None,
    language: str | None = None,
    edition: str | None = None,
    parser_version: str | None = None,
) -> DocumentRow:
    """Register a source file. Idempotent: same content -> same doc_id."""
    h = file_hash(path)
    fmt = detect_format(path)
    doc_id = stable_id("doc", h, fmt.value)
    with SessionLocal() as s:
        existing = s.get(DocumentRow, doc_id)
        if existing is not None:
            return existing
        row = DocumentRow(
            doc_id=doc_id,
            content_hash=h,
            format=fmt.value,
            source_path=str(Path(path).resolve()),
            title=title,
            author=author,
            translator=translator,
            language=language,
            edition=edition,
            parser_version=parser_version,
        )
        s.add(row)
        s.commit()
        return row
