"""Chapter-framework recall from persisted chapter paths (lexical, no reindex).

Derives the TOC tree from distinct ``(doc_id, chapter_path)`` pairs already
stored on evidence units; scores chapters by term overlap with the query.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from paknow.db import SessionLocal
from paknow.db.models import DocumentRow, EvidenceUnitRow
from paknow.retrieval.tokenize import sparse_weights


@lru_cache(maxsize=1)
def _chapter_index() -> tuple[dict[str, str], dict[str, list[tuple[str, ...]]]]:
    with SessionLocal() as s:
        docs = {d.doc_id: d.title or d.doc_id for d in s.query(DocumentRow).all()}
        rows = (
            s.query(EvidenceUnitRow.doc_id, EvidenceUnitRow.chapter_path)
            .distinct()
            .all()
        )
    chapters: dict[str, list[tuple[str, ...]]] = {}
    for doc_id, path in rows:
        if path:
            chapters.setdefault(doc_id, []).append(tuple(path))
    return docs, chapters


def toc_recall(query: str, top_k: int = 8) -> list[dict[str, Any]]:
    """Return top chapter paths whose titles overlap the query terms."""
    qtoks = set(sparse_weights(query))
    if not qtoks:
        return []
    docs, chapters = _chapter_index()
    scored: list[tuple[float, str, tuple[str, ...]]] = []
    for doc_id, paths in chapters.items():
        for path in paths:
            toks = set(sparse_weights(" ".join(path)))
            overlap = len(qtoks & toks)
            if overlap == 0:
                continue
            # deeper, more specific paths weigh slightly more
            score = overlap * (1.0 + 0.15 * len(path))
            scored.append((score, doc_id, path))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {
            "doc_id": doc_id,
            "doc_title": docs.get(doc_id, doc_id),
            "chapter_path": list(path),
            "title": " › ".join(path),
            "score": round(score, 2),
        }
        for score, doc_id, path in scored[:top_k]
    ]
