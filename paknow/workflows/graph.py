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

from paknow.memory.sop import select_sop
from paknow.memory.trace import save_trace
from paknow.retrieval.pipeline import retrieve_evidence
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
    # graph expansion output (non-citable) and expansion progress
    graph_context: list[dict[str, Any]]
    graph_evidence: list[dict[str, Any]]
    expanded_concepts: list[str]
    toc_context: list[dict[str, Any]]
    context_units: list[dict[str, Any]]
    barrefs: list[dict[str, Any]]
    acc: dict[str, dict[str, Any]]
    wants_examples: bool


def _route_node(state: QState) -> QState:
    plan = route(state["query"], state.get("images"))
    state["plan"] = plan
    state["intent"] = plan.intent
    state["is_framework"] = plan.is_framework
    state["routes"] = plan.routes
    state["wants_examples"] = plan.wants_examples
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

    result = retrieve_evidence(
        state["query"],
        queries,
        needs=state.get("evidence_needs", []),
        routes=state.get("routes", []),
        wants_examples=state.get("wants_examples", False),
        acc=state.get("acc", {}),
        expanded_concepts=state.get("expanded_concepts", []),
    )
    state["acc"] = result.acc
    state["hits"] = result.text_hits
    state["figures"] = result.figures
    state["context_units"] = result.context_units
    state["toc_context"] = result.toc_context
    state["barrefs"] = result.barrefs
    state["graph_context"] = result.graph_context
    state["graph_evidence"] = result.graph_evidence
    state["expanded_concepts"] = result.expanded_concepts
    state.setdefault("failures", []).extend(result.failures)
    state["round"] = state.get("round", 0) + 1
    return state


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
    # graph expansion stays a separate understanding aid: it is rendered in its
    # own prompt block and never joins the citable units / cited indices
    units = [h for h in state.get("hits", []) if h.get("evidence_id")]
    have = {u["evidence_id"] for u in units}
    for u in state.get("context_units", []):
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
        wants_examples=state.get("wants_examples", False),
        figure_catalog=[
            {"figure_id": f.get("figure_id"), "figure_label": f.get("figure_label")}
            for f in state.get("figures", [])
        ],
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
    ans = final.get("answer") or {}
    # only figures the answer actually chose to show (analysis-first, not a gallery)
    cited_figs = set(ans.get("figure_ids") or [])
    figures = [f for f in final.get("figures", []) if f.get("figure_id") in cited_figs]
    return {
        "run_id": final["run_id"],
        "intent": final.get("intent"),
        "is_framework": final.get("is_framework", False),
        "routes": final.get("routes", []),
        "wants_examples": final.get("wants_examples", False),
        "hits": final.get("hits", []),
        "graph_context": final.get("graph_context", []),
        "figures": figures,
        "barrefs": final.get("barrefs", []),
        "answer": final.get("answer"),
    }
