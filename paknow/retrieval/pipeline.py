"""Retrieval seam: one call returns the evidence partitions an answer needs.

The LangGraph retrieval node is a thin adapter over :func:`retrieve_evidence`.
Keeping the recall logic here makes retrieval independently callable (offline
evaluation, MCP inspection) and gives every assertion a single return value.

Partitions are explicit on :class:`RetrievalResult`:

- ``text_hits``: scored text recall, the only rank order the answer uses and
  the only channel that produces citable citations;
- ``graph_evidence`` / ``graph_context``: graph expansion output (empty until
  the expansion step is wired);
- ``context_units``: parent/adjacent units fetched for reading context;
- ``figures`` / ``toc_context`` / ``barrefs``: auxiliary recall;
- ``failures``: non-fatal failures; retrieval never raises for one bad step.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from paknow.db import SessionLocal
from paknow.db.models import FigureAssetRow
from paknow.retrieval.barref import barref_lookup
from paknow.retrieval.search import (
    caption_search,
    diversify,
    expand_evidence,
    hybrid_search,
    point_to_doc,
    rerank,
)
from paknow.retrieval.toc import toc_recall


@dataclass
class RetrievalResult:
    """Everything one retrieval round produced, partitioned by use."""

    text_hits: list[dict[str, Any]] = field(default_factory=list)
    graph_evidence: list[dict[str, Any]] = field(default_factory=list)
    graph_context: dict[str, Any] = field(default_factory=dict)
    context_units: list[dict[str, Any]] = field(default_factory=list)
    figures: list[dict[str, Any]] = field(default_factory=list)
    toc_context: list[dict[str, Any]] = field(default_factory=list)
    barrefs: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    acc: dict[str, dict[str, Any]] = field(default_factory=dict)
    query_count: int = 0


def retrieve_evidence(
    query: str,
    queries: Sequence[str],
    *,
    needs: Sequence[str] = (),
    routes: Sequence[str] = (),
    wants_examples: bool = False,
    acc: Mapping[str, dict[str, Any]] | None = None,
) -> RetrievalResult:
    """Run one retrieval round over ``queries`` (max 6) and return partitions.

    ``acc`` accumulates hits across rounds so bounded re-retrieval widens the
    candidate set instead of replacing it.
    """
    failures: list[str] = []
    seen: dict[str, dict[str, Any]] = dict(acc or {})

    round_queries: list[str] = []
    for q in queries:
        q = (q or "").strip()
        if q and q not in round_queries:
            round_queries.append(q)

    for q in round_queries[:6]:
        try:
            for p in hybrid_search(q, top_k=40):
                d = point_to_doc(p)
                eid = d.get("evidence_id")
                if eid and eid not in seen:
                    seen[eid] = d
        except Exception as e:  # noqa: BLE001
            failures.append(f"retrieve: {e}")

    hits = sorted(seen.values(), key=lambda x: x.get("score") or 0.0, reverse=True)
    # cross-encoder rerank over the top candidates (falls back to RRF order)
    if len(hits) >= 2:
        try:
            hits = rerank(query, hits[:20], top_k=20)
            for h in hits:
                h["reranked"] = True
        except Exception as e:  # noqa: BLE001
            failures.append(f"rerank: {e}")
    hits = diversify(hits, top_k=14, max_per_doc=5)

    ev_ids = [h["evidence_id"] for h in hits if h.get("evidence_id")]
    figures: list[dict[str, Any]] = []
    context_units: list[dict[str, Any]] = []
    try:
        expanded = expand_evidence(ev_ids)
        figures = list(expanded.get("figures", []))
        by_id = {u["evidence_id"]: u for u in expanded.get("units", [])}
        for h in hits:
            full = by_id.get(h.get("evidence_id"))
            if full:
                h.update(full)
        context_units = [
            u
            for u in expanded.get("units", [])
            if u.get("is_parent") or u.get("is_adjacent")
        ]
    except Exception as e:  # noqa: BLE001
        failures.append(f"expand_evidence: {e}")

    toc_context: list[dict[str, Any]] = []
    if "toc" in routes or "toc" in needs:
        try:
            toc_context = toc_recall(query)
        except Exception as e:  # noqa: BLE001
            failures.append(f"toc_recall: {e}")

    barrefs: list[dict[str, Any]] = []
    try:
        barrefs = barref_lookup(query)
    except Exception as e:  # noqa: BLE001
        failures.append(f"barref_lookup: {e}")
    if barrefs:
        try:
            figures = _merge_figures(figures, {b["figure_id"] for b in barrefs if b.get("figure_id")})
        except Exception as e:  # noqa: BLE001
            failures.append(f"barref_figures: {e}")

    # "show me examples": recall caption units so the answer can pick real
    # figures by id (their figure_ids feed the catalog)
    if wants_examples:
        try:
            cap_fig_ids: set[str] = set()
            for p in caption_search(query, top_k=10):
                d = point_to_doc(p)
                cap_fig_ids.update(d.get("figure_ids") or [])
            if cap_fig_ids:
                figures = _merge_figures(figures, cap_fig_ids)
        except Exception as e:  # noqa: BLE001
            failures.append(f"caption_search: {e}")

    return RetrievalResult(
        text_hits=hits,
        graph_evidence=[],
        graph_context={},
        context_units=context_units,
        figures=figures,
        toc_context=toc_context,
        barrefs=barrefs,
        failures=failures,
        acc=seen,
        query_count=len(round_queries),
    )


def _merge_figures(figures: list[dict[str, Any]], fig_ids: set[str]) -> list[dict[str, Any]]:
    """Attach figure rows (for the catalog / viewer) by id, preserving order."""
    have = {f.get("figure_id") for f in figures}
    missing = fig_ids - have
    if not missing:
        return figures
    with SessionLocal() as s:
        rows = s.query(FigureAssetRow).filter(FigureAssetRow.figure_id.in_(missing)).all()
    return figures + [
        {
            "figure_id": f.figure_id,
            "rel_path": f.rel_path,
            "width_px": f.width_px,
            "height_px": f.height_px,
            "figure_label": f.figure_label,
        }
        for f in rows
    ]
