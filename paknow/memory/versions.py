"""Component version snapshot for SOP compatibility checks (plan §9.6).

Cheap aggregate fingerprint of the corpus + graph plus the model configs in
use. Cached briefly; recomputed on reindex/model change.
"""
from __future__ import annotations

import hashlib
import time

from sqlalchemy import func

from paknow.config import settings
from paknow.db import SessionLocal
from paknow.db.models import EvidenceUnitRow, GraphEdgeRow, GraphNodeRow

_TTL = 60.0
_cache: tuple[float, dict[str, str]] | None = None


def component_versions() -> dict[str, str]:
    global _cache
    now = time.time()
    if _cache and now - _cache[0] < _TTL:
        return _cache[1]
    with SessionLocal() as s:
        n_units = s.query(func.count(EvidenceUnitRow.evidence_id)).scalar() or 0
        latest = s.query(func.max(EvidenceUnitRow.parsed_at)).scalar()
        n_nodes = s.query(func.count(GraphNodeRow.node_id)).scalar() or 0
        n_edges = s.query(func.count(GraphEdgeRow.edge_id)).scalar() or 0
    fingerprint = hashlib.sha1(f"{n_units}:{latest}".encode()).hexdigest()[:12]
    versions = {
        "corpus": fingerprint,
        "graph": f"{n_nodes}n/{n_edges}e",
        "embed_model": settings.embed_model,
        "rerank_model": settings.rerank_model,
        "visual_model": settings.visual_model,
        "generator_model": settings.deepseek_model,
    }
    _cache = (now, versions)
    return versions
