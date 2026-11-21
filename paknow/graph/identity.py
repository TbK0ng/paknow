"""Concept identity: surface form -> canonical concept id.

``configs/terms.yaml`` is the source of identity for the PA graph and lexical
retrieval. Unresolved names in the curated graph sources cannot create nodes.
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
    """Resolve canonical ids first, then unambiguous aliases."""
    terms = load_terms()
    out = {_norm(term.canonical): term for term in terms}
    aliases: dict[str, dict[str, Term]] = {}
    for term in terms:
        for alias in term.aliases:
            aliases.setdefault(_norm(alias), {})[term.canonical] = term
    for key, matches in aliases.items():
        if len(matches) == 1:
            out.setdefault(key, next(iter(matches.values())))
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
