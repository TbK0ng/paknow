"""Document registration and version manifest (plan §5.1)."""
from __future__ import annotations

from pathlib import Path

from paknow.config import settings
from paknow.db import SessionLocal
from paknow.db.models import DocumentRow
from paknow.evidence.contracts import DocFormat
from paknow.ids import file_hash, stable_id


def resolve_source(name: str) -> Path:
    """Locate a source file: as given, then under ``settings.corpus_dir``."""
    path = Path(name).expanduser()
    if path.is_absolute():
        return path
    if path.exists():
        return path
    return (settings.corpus_dir / path).resolve()


def source_path_for(path: Path) -> str:
    """Store paths relative to the corpus dir; keep absolutes from elsewhere."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(settings.corpus_dir.resolve()).as_posix()
    except ValueError:
        return str(resolved)


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
    """Register a source file. Idempotent: same content -> same doc_id.

    The stored ``source_path`` is relative to ``settings.corpus_dir`` when the
    file lives under it, and is refreshed when the same document is seen at a
    new location.
    """
    resolved = resolve_source(path)
    h = file_hash(str(resolved))
    fmt = detect_format(str(resolved))
    doc_id = stable_id("doc", h, fmt.value)
    with SessionLocal() as s:
        existing = s.get(DocumentRow, doc_id)
        if existing is not None:
            stored = source_path_for(resolved)
            if existing.source_path != stored:
                existing.source_path = stored
                s.commit()
            return existing
        row = DocumentRow(
            doc_id=doc_id,
            content_hash=h,
            format=fmt.value,
            source_path=source_path_for(resolved),
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
