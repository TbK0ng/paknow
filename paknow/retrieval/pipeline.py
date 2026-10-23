"""Retrieval seam: one call returns the evidence partitions an answer needs.

The LangGraph retrieval node is a thin adapter over :func:`retrieve_evidence`.
Keeping the recall logic here makes retrieval independently callable (offline
evaluation, MCP inspection) and gives every assertion a single return value.

Partitions are explicit on :class:`RetrievalResult`:

- ``text_hits``: scored text recall, the only rank order the answer uses and
  the only channel that produces citable citations;
- ``graph_evidence``: graph expansion entries, accumulated across gap rounds.
  They are understanding aids only: ``provenance="graph"``,
  ``non_citable=True``, and never merged into the citable units the answer
  indexes;
- ``context_units``: parent/adjacent units fetched for reading context;
- ``citable_units`` / ``citation_index``: the exact list generation renders and
  the evidence-id -> ``[n]`` binding derived from it, so generation never
  re-derives the ordering rule;
- ``figures`` / ``toc_context`` / ``barrefs``: auxiliary recall;
- ``failures``: non-fatal failures; retrieval never raises for one bad step.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from paknow.db import SessionLocal
from paknow.db.models import FigureAssetRow
from paknow.graph import expansion
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
    context_units: list[dict[str, Any]] = field(default_factory=list)
    citable_units: list[dict[str, Any]] = field(default_factory=list)
    citation_index: dict[str, int] = field(default_factory=dict)
    figures: list[dict[str, Any]] = field(default_factory=list)
    toc_context: list[dict[str, Any]] = field(default_factory=list)
    barrefs: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    acc: dict[str, dict[str, Any]] = field(default_factory=dict)
    expanded_concepts: list[str] = field(default_factory=list)
    graph_stats: dict[str, Any] = field(default_factory=dict)
    query_count: int = 0


def citable_units(
    text_hits: Sequence[Mapping[str, Any]],
    context_units: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Text hits then new context units: exactly what generation renders."""
    units: list[dict[str, Any]] = [
        dict(hit) for hit in text_hits if hit.get("evidence_id")
    ]
    have = {str(unit["evidence_id"]) for unit in units}
    for unit in context_units:
        evidence_id = unit.get("evidence_id")
        if evidence_id and str(evidence_id) not in have:
            units.append(dict(unit))
            have.add(str(evidence_id))
    return units


def citation_binding(units: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Evidence id -> rendered ``[n]`` index; derived observations are unnumbered."""
    index: dict[str, int] = {}
    position = 0
    for unit in units:
        if unit.get("provenance") == "derived_observation":
            continue
        evidence_id = unit.get("evidence_id")
        if evidence_id and str(evidence_id) not in index:
            index[str(evidence_id)] = position
        position += 1
    return index


def retrieve_evidence(
    query: str,
    queries: Sequence[str],
    *,
    needs: Sequence[str] = (),
    routes: Sequence[str] = (),
    wants_examples: bool = False,
    acc: Mapping[str, dict[str, Any]] | None = None,
    expanded_concepts: Sequence[str] = (),
    prior_graph_evidence: Sequence[Mapping[str, Any]] = (),
    expand_graph: bool = True,
) -> RetrievalResult:
    """Run one retrieval round over ``queries`` (max 6) and return partitions.

    ``acc`` accumulates hits across rounds so bounded re-retrieval widens the
    candidate set instead of replacing it. ``expanded_concepts`` carries the
    concepts already expanded, so a gap round only follows new ones.
    ``prior_graph_evidence`` is the graph block accumulated by earlier rounds;
    this round appends only new evidence ids and never drops it.

    ``expand_graph=False`` turns the graph layer off for this round: no seed
    query is submitted, nothing is expanded, ``graph_evidence`` stays empty,
    and ``graph_stats["enabled"]`` is false. The text path is unchanged, so the
    production-parity evaluation can subtract the two arms.
    """
    failures: list[str] = []
    seen: dict[str, dict[str, Any]] = dict(acc or {})

    round_queries: list[str] = []
    for q in queries:
        q = (q or "").strip()
        if q and q not in round_queries:
            round_queries.append(q)

    # query concepts are seeded first and resolved while text recall runs;
    # gap-round queries are included so a new concept can drive expansion.
    # With the graph layer off nothing is seeded, so the seed query is not
    # submitted at all.
    query_seeds: list[str] = []
    pool: ThreadPoolExecutor | None = None
    seed_future = None
    if expand_graph:
        pool = ThreadPoolExecutor(max_workers=1)
        seed_future = pool.submit(
            expansion.seed_concepts, " ".join(round_queries) or query
        )
    try:
        for q in round_queries[:6]:
            try:
                for p in hybrid_search(q, top_k=40):
                    d = point_to_doc(p)
                    eid = d.get("evidence_id")
                    if eid and eid not in seen:
                        seen[eid] = d
            except Exception as e:  # noqa: BLE001
                failures.append(f"retrieve: {e}")
        if seed_future is not None:
            try:
                query_seeds = seed_future.result()
            except Exception as e:  # noqa: BLE001
                failures.append(f"graph_seed: {e}")
    finally:
        if pool is not None:
            pool.shutdown()

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

    # citation indices must match generate()'s rendered unit list exactly
    units = citable_units(hits, context_units)
    seen_index = citation_binding(units)

    prior_expanded = list(dict.fromkeys(expanded_concepts))
    base_stats = dict(expansion.PlannedContext().stats)
    graph_entries: list[dict[str, Any]] = []
    if not expand_graph:
        # the graph layer is off, not empty: no seeds, no expansion, no graph
        # failures, and stats say so explicitly
        graph_stats = {
            **base_stats,
            "selected": 0,
            "seeds": 0,
            "accumulated": 0,
            "enabled": False,
        }
    else:
        new_seeds = expansion.select_new_seeds(
            query_seeds=query_seeds,
            text_units=hits,
            expanded=prior_expanded,
        )
        graph_seen: set[str] = set()
        for entry in prior_graph_evidence:
            evidence_id = entry.get("evidence_id")
            key = str(evidence_id) if evidence_id else None
            if key is not None and key in graph_seen:
                continue
            graph_entries.append(dict(entry))
            if key is not None:
                graph_seen.add(key)

        if new_seeds:
            plan, graph_failures = expansion.expand(new_seeds, seen_index=seen_index)
            failures.extend(graph_failures)
            for entry in plan.entries:
                key = str(entry.get("evidence_id"))
                if key in graph_seen:
                    continue
                graph_entries.append(entry)
                graph_seen.add(key)
            graph_stats = {
                **plan.stats,
                "seeds": len(new_seeds),
                "accumulated": len(graph_entries),
                "enabled": True,
            }
            prior_expanded = list(dict.fromkeys([*prior_expanded, *new_seeds]))
        else:
            # a round with no new concepts pays for nothing and must still report
            # stats, so cache-hit calls do not look like missing instrumentation
            graph_stats = {
                **base_stats,
                "selected": len(graph_entries),
                "seeds": 0,
                "accumulated": len(graph_entries),
                "enabled": True,
            }

    return RetrievalResult(
        text_hits=hits,
        graph_evidence=graph_entries,
        context_units=context_units,
        citable_units=units,
        citation_index=seen_index,
        figures=figures,
        toc_context=toc_context,
        barrefs=barrefs,
        failures=failures,
        acc=seen,
        expanded_concepts=prior_expanded,
        graph_stats=graph_stats,
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
