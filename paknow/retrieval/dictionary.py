"""Bilingual term dictionary: alias resolution + query expansion (plan §5.5).

Source of truth is ``configs/terms.yaml``; entries are ``pending`` human review
but usable for query expansion. Keeps the original query phrasing and adds
canonical + English/Chinese aliases rather than rewriting the query.
"""
from __future__ import annotations

import re
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
    out: dict[str, Term] = {}
    for t in data:
        canonical = t["canonical"]
        aliases = list(t.get("aliases", []))
        if canonical in out:  # merge duplicate canonical blocks
            merged = set(out[canonical].aliases) | set(aliases)
            out[canonical].aliases = list(merged)
            if t.get("ambiguity"):
                out[canonical].ambiguity = t["ambiguity"]
            if t.get("context"):
                out[canonical].context = t["context"]
        else:
            out[canonical] = Term(
                canonical=canonical,
                aliases=aliases,
                ambiguity=t.get("ambiguity"),
                context=t.get("context"),
            )
    return list(out.values())


def _alias_hit(text: str, alias: str) -> bool:
    """Alias found in text? ASCII aliases must sit on token boundaries, so
    ``TR`` does not match inside ``trading`` and ``H2`` not inside ``H20``."""
    al = alias.lower()
    if not al:
        return False
    if al[0].isascii():
        low = text.lower()
        for m in re.finditer(re.escape(al), low):
            s, e = m.start(), m.end()
            before = low[s - 1] if s > 0 else ""
            after = low[e] if e < len(low) else ""
            if not (before.isalnum() or after.isalnum()):
                return True
        return False
    return al in text.lower()


@lru_cache(maxsize=1)
def match_terms(text: str) -> dict[str, Term]:
    """Boundary-aware term matching: canonical -> matched Term (longest alias wins)."""
    hits: dict[str, Term] = {}
    for term in load_terms():
        for a in sorted(term.aliases, key=len, reverse=True):
            if _alias_hit(text, a):
                if term.canonical not in hits or len(a) > len(hits[term.canonical].aliases[0]):
                    hits[term.canonical] = term
                break
    return hits


def resolve(alias: str) -> Term | None:
    hits = match_terms(alias)
    for term in hits.values():
        if any(a.lower() == alias.lower() for a in term.aliases):
            return term
    return None


def expand_query(text: str) -> dict[str, list[str]]:
    """Return {'canonical', 'zh', 'en', 'disambig'} terms found in text.

    Aliases matched in the query are expanded to canonical id + every alias so
    lexical/dense recall can match either language without rewriting the query.
    ``disambig`` carries context phrases for ambiguous abbreviations (e.g. BTC)
    to bias retrieval toward the price-action sense.
    """
    hits = match_terms(text)
    zh: list[str] = []
    en: list[str] = []
    canon: list[str] = []
    disambig: list[str] = []
    for term in hits.values():
        canon.append(term.canonical)
        for a in term.aliases:
            if any("\u4e00" <= ch <= "\u9fff" for ch in a):
                zh.append(a)
            else:
                en.append(a)
        if term.ambiguity and term.context:
            disambig.append(term.context)
    return {
        "canonical": sorted(set(canon)),
        "zh": sorted(set(zh)),
        "en": sorted(set(en)),
        "disambig": sorted(set(disambig)),
    }
