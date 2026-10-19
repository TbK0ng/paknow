"""Sparse tokenization for lexical recall (plan §5.5).

Protects domain tokens (``H2``, ``EMA20``, ``图 4.5``, ``BTC``, ``STC``) from
being split; segments Chinese via jieba with the term dictionary as userdict.
"""
from __future__ import annotations

import re
from collections import Counter
from functools import lru_cache

from paknow.retrieval.dictionary import load_terms, match_terms

_PROTECT_RE = re.compile(
    r"(?i)\b(?:EMA\s*\d*|H\d+|L\d+|图\s*\d+(?:\.\d+)?|Figure\s*\d+(?:\.\d+)?|BTC|STC)\b"
)
_EN_RE = re.compile(r"[a-z][a-z0-9]{1,}")
_ZH_RE = re.compile(r"[\u4e00-\u9fff]+")


@lru_cache(maxsize=1)
def _jieba():
    import jieba

    for term in load_terms():
        for a in term.aliases:
            jieba.add_word(a)
    return jieba


def _tokens(text: str) -> list[str]:
    toks: list[str] = []
    lower = text.lower()
    # dictionary aliases -> canonical concept id (boundary-aware matching)
    toks.extend(match_terms(text))
    # protected numeric/abbreviation patterns
    toks.extend(m.group(0).replace(" ", "") for m in _PROTECT_RE.finditer(text))
    # English words
    toks.extend(_EN_RE.findall(lower))
    # Chinese words
    jb = _jieba()
    for seg in _ZH_RE.findall(text):
        toks.extend(w.strip() for w in jb.cut(seg) if w.strip())
    return toks


def sparse_weights(text: str) -> dict[str, float]:
    """Term-frequency sparse vector (float weights)."""
    c = Counter(_tokens(text))
    return dict(c)


def sparse_indices_values(text: str) -> tuple[list[int], list[float]]:
    """Return (indices, values) using a stable vocabulary hash.

    Token -> index via 32-bit FNV-1a hash mod 2**24; collision-tolerant for
    lexical recall. Values are raw term frequencies.
    """
    w = sparse_weights(text)
    idx: list[int] = []
    vals: list[float] = []
    for token, tf in w.items():
        idx.append(_fnv(token))
        vals.append(float(tf))
    return idx, vals


def _fnv(token: str) -> int:
    h = 0x811C9DC5
    for b in token.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h % (1 << 24)
