"""Bilingual lexical terms and aliases shared by BM25 and graph identity."""
from __future__ import annotations

from hashlib import sha256
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
    kind: str | None = None


@lru_cache(maxsize=1)
def load_terms() -> list[Term]:
    data = yaml.safe_load(_TERMS_PATH.read_text(encoding="utf-8")) or []
    out: dict[str, Term] = {}
    for t in data:
        canonical = t["canonical"]
        aliases = list(t.get("aliases", []))
        if canonical in out:
            merged = set(out[canonical].aliases) | set(aliases)
            out[canonical].aliases = list(merged)
            if t.get("ambiguity"):
                out[canonical].ambiguity = t["ambiguity"]
            if t.get("context"):
                out[canonical].context = t["context"]
        else:
            out[canonical] = Term(
                canonical=canonical, aliases=aliases, ambiguity=t.get("ambiguity"),
                context=t.get("context"), kind=t.get("kind"),
            )
    return list(out.values())


def dictionary_digest() -> str:
    return sha256(_TERMS_PATH.read_bytes()).hexdigest()


def _alias_hit(lower: str, alias: str) -> bool:
    """ASCII aliases sit on token boundaries (H2 is not H20)."""
    if not alias:
        return False
    start = lower.find(alias)
    if not alias[0].isascii():
        return start >= 0
    while start >= 0:
        end = start + len(alias)
        if ((start == 0 or not lower[start - 1].isalnum())
                and (end == len(lower) or not lower[end].isalnum())):
            return True
        start = lower.find(alias, end)
    return False


@lru_cache(maxsize=1)
def _term_aliases() -> tuple[tuple[Term, tuple[str, ...]], ...]:
    return tuple((term, tuple(a.lower() for a in sorted(term.aliases, key=len, reverse=True)))
                 for term in load_terms())


@lru_cache(maxsize=1)
def match_terms(text: str) -> dict[str, Term]:
    """Boundary-aware term matching: canonical -> matched Term."""
    hits: dict[str, Term] = {}
    lower = text.lower()
    for term, aliases in _term_aliases():
        for a in aliases:
            if _alias_hit(lower, a):
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
