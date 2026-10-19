"""Bilingual term dictionary: alias resolution + query expansion (plan §5.5).

Source of truth is ``configs/terms.yaml``; entries are ``pending`` human review
but usable for query expansion. Keeps the original query phrasing and adds
canonical + English/Chinese aliases rather than rewriting the query.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

_TERMS_PATH = Path(__file__).resolve().parent.parent.parent / "configs" / "terms.yaml"


@dataclass
class Term:
    canonical: str
    aliases: list[str] = field(default_factory=list)
    ambiguity: str | None = None
    context: str | None = None


@lru_cache(maxsize=1)
def load_terms() -> list[Term]:
    data = yaml.safe_load(_TERMS_PATH.read_text(encoding="utf-8")) or []
    return [
        Term(
            canonical=t["canonical"],
            aliases=list(t.get("aliases", [])),
            ambiguity=t.get("ambiguity"),
            context=t.get("context"),
        )
        for t in data
    ]


@lru_cache(maxsize=1)
def _alias_index() -> dict[str, Term]:
    idx: dict[str, Term] = {}
    for term in load_terms():
        for a in term.aliases:
            key = a.lower()
            if key not in idx or len(a) > len(idx[key].aliases[0]):
                idx[key] = term
    return idx


def resolve(alias: str) -> Term | None:
    return _alias_index().get(alias.lower())


def expand_query(text: str) -> dict[str, list[str]]:
    """Return {'canonical': [..], 'zh': [..], 'en': [..]} terms found in text.

    Aliases matched in the query are expanded to canonical id + every alias so
    lexical/dense recall can match either language without rewriting the query.
    """
    hit: dict[str, Term] = {}
    lower = text.lower()
    for key, term in _alias_index().items():
        if key in lower:
            hit[term.canonical] = term

    zh: list[str] = []
    en: list[str] = []
    canon: list[str] = []
    for term in hit.values():
        canon.append(term.canonical)
        for a in term.aliases:
            if any("\u4e00" <= ch <= "\u9fff" for ch in a):
                zh.append(a)
            else:
                en.append(a)
    return {"canonical": sorted(set(canon)), "zh": sorted(set(zh)), "en": sorted(set(en))}
