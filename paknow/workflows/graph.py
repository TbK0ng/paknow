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

from paknow.memory.trace import save_trace
from paknow.retrieval.search import expand_evidence, hybrid_search, point_to_doc
from paknow.workflows.evidence import check_sufficiency
from paknow.workflows.generate import generate
from paknow.workflows.router import QueryPlan, retrieval_queries, route


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
    return state


def _retrieve_node(state: QState) -> QState:
    queries = retrieval_queries(state["plan"], state["query"])
    if state.get("next_query"):
        queries.append(state["next_query"])
    seen: dict[str, dict[str, Any]] = {}
    for q in queries[:6]:
        try:
            for p in hybrid_search(q, top_k=40):
                d = point_to_doc(p)
                eid = d.get("evidence_id")
                if eid and eid not in seen:
                    seen[eid] = d
        except Exception as e:  # noqa: BLE001
            state.setdefault("failures", []).append(f"retrieve: {e}")
    hits = sorted(seen.values(), key=lambda x: x.get("score") or 0.0, reverse=True)[:14]
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
    return state


def _check_node(state: QState) -> QState:
    suff = check_sufficiency(
        state["query"],
        state.get("evidence_needs", []),
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
    state["answer"] = generate(
        state["query"], state.get("hits", []), state.get("images"), mode=mode
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
    }
    final = _graph.invoke(state)
    return {
        "run_id": final["run_id"],
        "intent": final.get("intent"),
        "is_framework": final.get("is_framework", False),
        "routes": final.get("routes", []),
        "hits": final.get("hits", []),
        "figures": final.get("figures", []),
        "answer": final.get("answer"),
    }
