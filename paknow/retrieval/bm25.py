"""BM25 vectors using PAknow's bilingual tokenizer and Qdrant corpus IDF."""
from __future__ import annotations

from math import isfinite
from collections import Counter
from typing import Mapping

from qdrant_client.models import SparseVector

from paknow.retrieval.tokenize import token_counts, term_id

VECTOR_NAME = "bm25_v1"
VERSION = "bm25-v1"
K1 = 1.2
B = 0.75


def lexical_counts(text: str) -> dict[int, int]:
    # Coalesce hashed collisions. Qdrant requires unique sparse indices.
    values: Counter[int] = Counter()
    for term, count in token_counts(text).items():
        values[term_id(term)] += count
    return dict(values)


def query_vector(text: str) -> SparseVector:
    """Score each distinct query term once; Qdrant supplies its corpus IDF."""
    indices = sorted(lexical_counts(text))
    return SparseVector(indices=indices, values=[1.0] * len(indices))


def document_vector(counts: Mapping[int, int], *, avgdl: float) -> SparseVector:
    """Compute the BM25 term-frequency saturation and length factor for one unit."""
    if not counts:
        return SparseVector(indices=[], values=[])
    if not isfinite(avgdl) or avgdl <= 0:
        raise ValueError("BM25 requires a positive average document length")
    length = sum(counts.values())
    normalizer = K1 * (1 - B + B * length / avgdl)
    indices = sorted(counts)
    weights = [counts[i] * (K1 + 1) / (counts[i] + normalizer) for i in indices]
    return SparseVector(indices=indices, values=weights)
