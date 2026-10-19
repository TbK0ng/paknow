"""Dense embedding via BGE-M3 (dense + sparse + colbert in one model).

Lazy-loaded so imports stay cheap; the model is fetched once on first use.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np

from paknow.config import settings


@lru_cache(maxsize=1)
def _model():
    from sentence_transformers import SentenceTransformer

    device = "cuda" if _cuda_ok() else "cpu"
    return SentenceTransformer(settings.embed_model, device=device)


def _cuda_ok() -> bool:
    try:
        import torch

        return torch.cuda.is_available()
    except Exception:
        return False


def embed_texts(texts: list[str]) -> np.ndarray:
    return _model().encode(
        texts,
        batch_size=settings.embed_batch_size,
        normalize_embeddings=True,
        show_progress_bar=False,
    )


def embed_query(text: str) -> np.ndarray:
    return _model().encode([text], normalize_embeddings=True)[0]
