"""LangGraph query state machine: route -> retrieve -> check -> (retry) -> generate -> record.

Routing now also performs query construction (step-back + multi-query + HyDE)
via ``QueryPlan``; framework questions get synthesis generation.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from paknow.db import SessionLocal
from paknow.db.models import FigureAssetRow
from paknow.graph.retrieve import concept_lookup, expand_evidence_ids, transition_chains
from paknow.memory.sop import select_sop
from paknow.memory.trace import save_trace
from paknow.retrieval.barref import barref_lookup
from paknow.retrieval.dictionary import expand_query
from paknow.retrieval.search import (
    diversify,
    expand_evidence,
    hybrid_search,
    point_to_doc,
    rerank,
)
from paknow.retrieval.toc import toc_recall
from paknow.workflows.evidence import check_sufficiency
from paknow.workflows.generate import generate
from paknow.workflows.router import QueryPlan, retrieval_queries, route
from paknow.workflows.sop_exec import apply_sop


class QState(TypedDict, total=False):
    query: str
    images: list[bytes]
    plan: QueryPlan
    intent: str
    is_framework: bool
    routes: list[str]
    evidence_needs: list[str]
    sub_queries: list[str]
    step_back: str
    hyde: str
    next_query: str | None
    hits: list[dict[str, Any]]
    figures: list[dict[str, Any]]
    round: int
    max_rounds: int
    sufficiency: dict[str, Any]
    answer: dict[str, Any]
    run_id: str
    t0: float
    failures: list[str]
    # SOP-driven retrieval directives (selected in route, applied in retrieve/check/generate)
    sop_id: str | None
    sop_status: str
    sop_extra_queries: list[str]
    sop_evidence_requirements: list[str]
    sop_fallback: str | None
    sop_stop: str | None
    sop_skipped_ops: list[str]
    # graph/toc recall contexts
    graph_context: dict[str, Any]
    graph_evidence: list[dict[str, Any]]
    toc_context: list[dict[str, Any]]
    context_units: list[dict[str, Any]]
    barrefs: list[dict[str, Any]]
    acc: dict[str, dict[str, Any]]


def _route_node(state: QState) -> QState:
    plan = route(state["query"], state.get("images"))
    state["plan"] = plan
    state["intent"] = plan.intent
    state["is_framework"] = plan.is_framework
    state["routes"] = plan.routes
    state["evidence_needs"] = plan.evidence_needs
    state["sub_queries"] = plan.sub_queries
    state["step_back"] = plan.step_back
    state["hyde"] = plan.hyde
    state["max_rounds"] = plan.max_search_rounds
    state["round"] = 0
    sop = select_sop(plan.intent)
    if sop:
        apply_sop(sop, state, state["query"])
    return state


def _retrieve_node(state: QState) -> QState:
    # targeted gap query first (drives this round), then SOP extras, then plan set
    queries: list[str] = []
    seen_q: set[str] = set()
    for q in [
        state.get("next_query"),
        *state.get("sop_extra_queries", []),
        *retrieval_queries(state["plan"], state["query"]),
    ]:
        q = (q or "").strip()
        if q and q not in seen_q:
            seen_q.add(q)
            queries.append(q)
    seen: dict[str, dict[str, Any]] = dict(state.get("acc", {}))
    for q in queries[:6]:
        try:
            for p in hybrid_search(q, top_k=40):
                d = point_to_doc(p)
                eid = d.get("evidence_id")
                if eid and eid not in seen:
                    seen[eid] = d
        except Exception as e:  # noqa: BLE001
            state.setdefault("failures", []).append(f"retrieve: {e}")
    state["acc"] = seen  # accumulate across retrieval rounds

    hits = sorted(seen.values(), key=lambda x: x.get("score") or 0.0, reverse=True)
    # cross-encoder rerank over the top candidates (falls back to RRF order)
    if len(hits) >= 2:
        try:
            hits = rerank(state["query"], hits[:20], top_k=20)
            for h in hits:
                h["reranked"] = True
        except Exception as e:  # noqa: BLE001
            state.setdefault("failures", []).append(f"rerank: {e}")
    hits = diversify(hits, top_k=14, max_per_doc=5)

    state["hits"] = hits
    state["round"] = state.get("round", 0) + 1
    ev_ids = [h["evidence_id"] for h in hits if h.get("evidence_id")]
    expanded = expand_evidence(ev_ids)
    state["figures"] = expanded.get("figures", [])
    by_id = {u["evidence_id"]: u for u in expanded.get("units", [])}
    for h in hits:
        full = by_id.get(h.get("evidence_id"))
        if full:
            h.update(full)
    state["context_units"] = [
        u
        for u in expanded.get("units", [])
        if u.get("is_parent") or u.get("is_adjacent")
    ]

    # graph recall: concepts/claims + transition cycles + claim-linked evidence
    routes = state.get("routes", [])
    needs = state.get("evidence_needs", [])
    if "graph_local" in routes or "cycle" in needs or state.get("is_framework"):
        terms = expand_query(state["query"])["canonical"]
        gctx: dict[str, Any] = {
            "concepts_claims": concept_lookup(terms + state.get("sub_queries", [])),
            "cycles": transition_chains(),
        }
        related = expand_evidence_ids(ev_ids)
        if related:
            gctx["related_claims"] = related
        state["graph_context"] = gctx
        rel_ids = [r["evidence_id"] for r in related if r.get("evidence_id")]
        if rel_ids:
            rel_exp = expand_evidence(rel_ids[:6], parent_limit=0, adjacent_limit=0)
            state["graph_evidence"] = rel_exp.get("units", [])

    # toc recall: chapter framework for structural questions
    if "toc" in routes or "toc" in needs:
        state["toc_context"] = toc_recall(state["query"])

    # bar-number references inside figures ("K线2/3构成双底")
    state["barrefs"] = barref_lookup(state["query"])
    if state["barrefs"]:
        _merge_barref_figures(state)
    return state


def _merge_barref_figures(state: QState) -> None:
    """Attach figures referenced by bar-refs to the figure list (viewer)."""
    fig_ids = {b["figure_id"] for b in state.get("barrefs", []) if b.get("figure_id")}
    if not fig_ids:
        return
    have = {f.get("figure_id") for f in state.get("figures", [])}
    missing = fig_ids - have
    if not missing:
        return
    with SessionLocal() as s:
        figs = (
            s.query(FigureAssetRow)
            .filter(FigureAssetRow.figure_id.in_(missing))
            .all()
        )
    for f in figs:
        state.setdefault("figures", []).append(
            {
                "figure_id": f.figure_id,
                "rel_path": f.rel_path,
                "width_px": f.width_px,
                "height_px": f.height_px,
                "figure_label": f.figure_label,
            }
        )


def _check_node(state: QState) -> QState:
    needs = list(state.get("evidence_needs", []))
    for req in state.get("sop_evidence_requirements", []):
        if req not in needs:
            needs.append(req)
    suff = check_sufficiency(
        state["query"],
        needs[:8],
        state.get("hits", []),
        round_=state.get("round", 0),
        max_rounds=state.get("max_rounds", 3),
    )
    state["sufficiency"] = suff.model_dump()
    state["next_query"] = suff.next_query
    return state


def _after_check(state: QState) -> str:
    suff = state.get("sufficiency", {})
    if suff.get("sufficient") or state.get("round", 0) >= state.get("max_rounds", 3):
        return "generate"
    return "retrieve"


def _generate_node(state: QState) -> QState:
    mode = "synthesis" if state.get("is_framework") else "extractive"
    units = [h for h in state.get("hits", []) if h.get("evidence_id")]
    have = {u["evidence_id"] for u in units}
    for src in ("graph_evidence", "context_units"):
        for u in state.get(src, []):
            if u.get("evidence_id") and u["evidence_id"] not in have:
                units.append(u)
                have.add(u["evidence_id"])
    state["answer"] = generate(
        state["query"],
        units,
        state.get("images"),
        mode=mode,
        graph_context=state.get("graph_context"),
        toc_context=state.get("toc_context"),
        fallback=state.get("sop_fallback"),
        barrefs=state.get("barrefs"),
    )
    return state


def _record_node(state: QState) -> QState:
    ans = state.get("answer") or {}
    ev_ids = ans.get("evidence_ids") or []
    save_trace(
        state["run_id"],
        state["query"],
        intents=[state.get("intent", "knowledge_qa")],
        routes=state.get("routes", []),
        evidence_ids=ev_ids,
        answer=ans,
        failures=state.get("failures", []),
        elapsed_ms=int((time.time() - state["t0"]) * 1000),
        sop_id=state.get("sop_id"),
    )
    # auto memory-optimization (non-blocking; runs the 评价→蒸馏→发布 loop)
    _maybe_auto_optimize()
    return state


def _maybe_auto_optimize() -> None:
    try:
        from paknow.memory import optimizer

        if optimizer.should_optimize():
            threading.Thread(target=optimizer.auto_optimize, daemon=True).start()
    except Exception:  # noqa: BLE001
        pass


def build_graph():
    g = StateGraph(QState)
    g.add_node("route", _route_node)
    g.add_node("retrieve", _retrieve_node)
    g.add_node("check", _check_node)
    g.add_node("generate", _generate_node)
    g.add_node("record", _record_node)
    g.add_edge(START, "route")
    g.add_edge("route", "retrieve")
    g.add_edge("retrieve", "check")
    g.add_conditional_edges("check", _after_check, {"generate": "generate", "retrieve": "retrieve"})
    g.add_edge("generate", "record")
    g.add_edge("record", END)
    return g.compile()


_graph = build_graph()


def run_query(query: str, images: list[bytes] | None = None) -> dict[str, Any]:
    state: QState = {
        "query": query,
        "images": images or [],
        "run_id": uuid.uuid4().hex,
        "t0": time.time(),
        "failures": [],
        "round": 0,
        "max_rounds": 3,
        "acc": {},
    }
    final = _graph.invoke(state)
    return {
        "run_id": final["run_id"],
        "intent": final.get("intent"),
        "is_framework": final.get("is_framework", False),
        "routes": final.get("routes", []),
        "hits": final.get("hits", []),
        "figures": final.get("figures", []),
        "barrefs": final.get("barrefs", []),
        "answer": final.get("answer"),
    }
