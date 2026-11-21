"""Populate the local PA Agent text index from user-supplied PDF/EPUB files."""
from __future__ import annotations

import argparse
from pathlib import Path

from paknow.config import settings
from paknow.db import engine, init_db
from paknow.evidence.contracts import DocFormat
from paknow.ingest.manifest import detect_format, resolve_source, verify_originals
from paknow.ingest.pipeline import ingest_epub, ingest_pdf
from paknow.retrieval.index import index_doc, rebuild_bm25
from paknow.retrieval.pa_corpus import MANIFEST
from paknow.retrieval.qdrant_store import check_store_ready, get_client, invalidate_bm25


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="+", help="PDF/EPUB path, absolute or under PAKNOW_CORPUS_DIR")
    args = parser.parse_args(argv)
    sources = {}
    for name in args.source:
        path = resolve_source(name).resolve()
        if not path.is_file():
            parser.error(f"source file not found: {path}")
        try:
            sources[path] = detect_format(str(path))
        except ValueError as exc:
            parser.error(str(exc))

    if settings.qdrant_url or engine.url.get_backend_name() != "sqlite" or not engine.url.database:
        parser.error("PA corpus builds require isolated SQLite and embedded Qdrant")
    database = Path(engine.url.database)
    if database.exists() or (settings.objects_dir / MANIFEST).exists():
        parser.error("choose a fresh SQLite file and objects directory; do not import over a PA store")
    try:
        verify_originals(sources)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    settings.ensure_dirs()
    client = get_client()  # Acquire the embedded-store lock before writing metadata.
    if client.get_collections().collections:
        parser.error("choose an empty objects directory for the new index")
    database.parent.mkdir(parents=True, exist_ok=True)
    invalidate_bm25()
    init_db()
    indexed = 0
    for path, kind in sources.items():
        ingest = ingest_pdf if kind == DocFormat.pdf else ingest_epub
        print(f"{path.name}: parsing", flush=True)
        doc, _ = ingest(str(path))
        count = index_doc(doc.doc_id)
        indexed += count
        print(f"{path.name}: indexed {count} text units", flush=True)
    if not indexed:
        parser.exit(1, "No text units indexed. Provide PDF/EPUB files with extractable text.\n")
    rebuild_bm25(client, progress=lambda line: print(line, flush=True))
    print(f"PA Agent text index ready: {check_store_ready(client)} points", flush=True)


if __name__ == "__main__":
    try:
        main()
    finally:
        if get_client.cache_info().currsize:
            get_client().close()
