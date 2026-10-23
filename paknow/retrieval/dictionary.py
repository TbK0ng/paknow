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
    # optional concept classification: term | pattern | regime | setup
    kind: str | None = None


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
                kind=t.get("kind"),
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


@dataclass(frozen=True)
class TermMatch:
    """One alias occurrence in a text, with offsets into the original text."""

    start: int
    end: int
    canonical: str
    alias: str


@lru_cache(maxsize=1)
def _alias_candidates() -> tuple[tuple[str, str], ...]:
    """All (alias, canonical) pairs, longest alias first for greedy matching."""
    rows = [(a, t.canonical) for t in load_terms() for a in t.aliases]
    rows.sort(key=lambda x: (-len(x[0]), x[0]))
    return tuple(rows)


def _occurrences(text: str, alias: str) -> list[tuple[int, int]]:
    al = alias.lower()
    if not al:
        return []
    if al[0].isascii():
        low = text.lower()
        out: list[tuple[int, int]] = []
        for m in re.finditer(re.escape(al), low):
            s, e = m.start(), m.end()
            before = low[s - 1] if s > 0 else ""
            after = low[e] if e < len(low) else ""
            if not (before.isalnum() or after.isalnum()):
                out.append((s, e))
        return out
    out = []
    low = text.lower()
    i = low.find(al)
    while i >= 0:
        out.append((i, i + len(al)))
        i = low.find(al, i + 1)
    return out


@lru_cache(maxsize=8192)
def find_terms(text: str) -> tuple[TermMatch, ...]:
    """All dictionary aliases in ``text``, longest-first and non-overlapping.

    Returns offsets so callers can split a phrase around concept mentions
    (heading grammar, claim subject normalization, evidence concept scanning).
    """
    if not text:
        return ()
    candidates: list[TermMatch] = []
    for alias, canonical in _alias_candidates():
        for start, end in _occurrences(text, alias):
            candidates.append(TermMatch(start, end, canonical, alias))
    candidates.sort(key=lambda m: (-(m.end - m.start), m.start, m.canonical))
    chosen: list[TermMatch] = []
    for cand in candidates:
        if any(cand.start < m.end and cand.end > m.start for m in chosen):
            continue
        chosen.append(cand)
    return tuple(sorted(chosen, key=lambda m: m.start))



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
