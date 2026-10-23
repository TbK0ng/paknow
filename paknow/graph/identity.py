"""Concept identity: surface form -> canonical concept id.

``configs/terms.yaml`` is the only source of identity. Extraction may produce
any phrasing ("High 2", "H2", "高二"); only forms that resolve to a dictionary
entry become concept nodes. Unresolved forms are queued for review instead of
creating nodes, so the graph cannot accumulate near-duplicates.
"""
from __future__ import annotations

import re
from functools import lru_cache

from paknow.ids import stable_id
from paknow.retrieval.dictionary import Term, load_terms


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).casefold()


def display_label(term: Term) -> str:
    """Chinese alias when the dictionary has one (book language), else canonical."""
    for alias in term.aliases:
        if any("\u4e00" <= ch <= "\u9fff" for ch in alias):
            return alias
    return term.canonical


@lru_cache(maxsize=1)
def _index() -> dict[str, Term]:
    """normalized canonical/alias -> Term."""
    out: dict[str, Term] = {}
    for term in load_terms():
        out.setdefault(_norm(term.canonical), term)
        for alias in term.aliases:
            out.setdefault(_norm(alias), term)
    return out


def resolve_term(surface: str | None) -> Term | None:
    if not surface:
        return None
    return _index().get(_norm(surface))


def canonical_for(surface: str | None) -> str | None:
    term = resolve_term(surface)
    return term.canonical if term else None


def label_for(canonical: str) -> str:
    term = resolve_term(canonical)
    return display_label(term) if term else canonical


def concept_kind_for(canonical: str) -> str | None:
    """Optional ``kind:`` on the dictionary entry (term/pattern/regime/setup)."""
    term = resolve_term(canonical)
    return term.kind if term else None


def concept_node_id(canonical: str) -> str:
    return stable_id("node", canonical)


def candidate_key(surface: str) -> str:
    """Stable key for an unresolved surface queued for human review."""
    return stable_id("cand", _norm(surface))
