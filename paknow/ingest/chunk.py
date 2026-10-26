"""Sentence-boundary chunking of over-long evidence units (plan §5.4).

Sub-chunks target ~600-1200 chars; a long unit is split into children sharing
the same locator, with ``parent_id`` set to the original unit id so sibling
context can be recovered at retrieval time.
"""
from __future__ import annotations

import re

from paknow.evidence.contracts import EvidenceUnit
from paknow.ids import content_hash, stable_id

_SENT_END = re.compile(r"(?<=[。！？!?；;])")

DEFAULT_MAX_CHARS = 1200


def chunk_text(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    parts = [p for p in _SENT_END.split(text) if p.strip()] or [text]
    chunks: list[str] = []
    cur = ""
    for p in parts:
        while len(p) > max_chars:  # a single sentence longer than the cap
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(p[:max_chars])
            p = p[max_chars:]
        if len(cur) + len(p) <= max_chars:
            cur += p
        else:
            chunks.append(cur)
            cur = p
    if cur.strip():
        chunks.append(cur)
    return chunks or [text]


def chunk_units(units: list[EvidenceUnit], max_chars: int = DEFAULT_MAX_CHARS) -> list[EvidenceUnit]:
    out: list[EvidenceUnit] = []
    for u in units:
        if len(u.text) <= max_chars:
            out.append(u)
            continue
        children = [
            u.model_copy(
                update={
                    "evidence_id": stable_id("ev", u.doc_id, u.evidence_id, str(j)),
                    "text": t,
                    "text_hash": content_hash(t.encode("utf-8")),
                    "parent_id": u.evidence_id,
                }
            )
            for j, t in enumerate(chunk_text(u.text, max_chars))
        ]
        # The parent stays as context-only reading material; only children enter
        # the search index, so a child hit can expand back to its whole unit.
        out.append(u.model_copy(update={"child_ids": [c.evidence_id for c in children]}))
        out.extend(children)
    return out
