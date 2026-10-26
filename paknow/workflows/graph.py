"""LangGraph query state machine: route -> read -> retrieve -> check -> (retry) -> generate -> record.

Routing now also performs query construction (step-back + multi-query + HyDE)
via ``QueryPlan``; framework questions get synthesis generation.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from typing import Any, Sequence, TypedDict

from langgraph.graph import END, START, StateGraph

from paknow.chart import chart_queries, is_non_chart, read_chart
from paknow.memory.sop import select_sop
from paknow.memory.trace import save_trace
from paknow.retrieval.pipeline import citable_units, citation_binding, retrieve_evidence
from paknow.workflows.evidence import check_sufficiency
from paknow.workflows.generate import generate
from paknow.workflows.router import PlanSource, QueryPlan, retrieval_queries, route
from paknow.workflows.sop_exec import apply_sop


class QState(TypedDict, total=False):
    query: str
    images: list[bytes]
    plan: QueryPlan
    # "frozen" when the caller injected the plan, "routed" otherwise
    plan_source: PlanSource
    # the query list retrieval actually submits, captured on the first round
    queries: list[str]
    intent: str
    is_framework: bool
    routes: list[str]
    evidence_needs: list[str]
    sub_queries: list[str]
    step_back: str
    hyde: str
    next_query: str | None
    hits: list[dict[str, Any]]
    # ranked recall before the answer's diversity cap: the judge's evidence pool
    candidate_hits: list[dict[str, Any]]
    figures: list[dict[str, Any]]
    round: int
    max_rounds: int
    sufficiency: dict[str, Any]
    judge_stats: dict[str, int]
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
    # graph expansion output (non-citable), accumulated across gap rounds
    graph_evidence: list[dict[str, Any]]
    graph_stats: dict[str, Any]
    expanded_concepts: list[str]
    expand_graph: bool
    citable_units: list[dict[str, Any]]
    citation_index: dict[str, int]
    toc_context: list[dict[str, Any]]
    context_units: list[dict[str, Any]]
    barrefs: list[dict[str, Any]]
    acc: dict[str, dict[str, Any]]
    wants_examples: bool
    # structured chart read (chart intent only); None when absent or failed
    chart_read: dict[str, Any] | None


def query_hash(queries: Sequence[str]) -> str:
    """Stable hash of an assembled query list: order-sensitive, JSON-encoded."""
    payload = json.dumps(list(queries), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def initial_state(
    query: str,
    *,
    images: list[bytes] | None = None,
    expand_graph: bool = True,
    plan: QueryPlan | None = None,
) -> QState:
    """The state every entry point starts from; ``plan`` is the eval seam."""
    state: QState = {
        "query": query,
        "images": images or [],
        "run_id": uuid.uuid4().hex,
        "t0": time.time(),
        "failures": [],
        "round": 0,
        "max_rounds": 3,
        "acc": {},
        "expand_graph": expand_graph,
    }
    if plan is not None:
        state["plan"] = plan
    return state


def _route_node(state: QState) -> QState:
    # an injected plan is the evaluation seam: the same fields are derived from
    # it, but the router model is not asked for one (answer-level evaluation
    # needs the query list frozen to compare code changes)
    plan = state.get("plan")
    state["plan_source"] = "frozen" if plan else "routed"
    plan = plan or route(state["query"], state.get("images"))
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


def _read_node(state: QState) -> QState:
    """One structured read, before retrieval: the sub-queries need it.

    Only a chart question with an upload has something to read; every other
    state passes through without a model call. A failed read is recorded here
    (the only place that appends it) and never raised, so retrieval falls back
    to the text path.
    """
    state["chart_read"] = None
    if state.get("intent") != "chart_analysis" or not state.get("images"):
        return state
    read, failure = read_chart(state["images"])
    if read is None:
        reason = (failure or {}).get("reason") or "no_content"
        detail = (failure or {}).get("detail")
        state.setdefault("failures", []).append(
            f"chart_read: {reason}" + (f" ({detail})" if detail else "")
        )
        return state
    state["chart_read"] = read.model_dump()
    return state


def _retrieve_node(state: QState) -> QState:
    # targeted gap query first (drives this round), then SOP extras, then plan set
    plan_queries = retrieval_queries(state["plan"], state["query"])
    if state.get("intent") == "chart_analysis":
        read_queries = chart_queries(state.get("chart_read"))
        if read_queries:
            # the read names the structure the picture shows, so its wording
            # takes the plan slots right after the question and shares the cap
            plan_queries = (plan_queries[:1] + read_queries + plan_queries[1:])[:6]
    queries: list[str] = []
    seen_q: set[str] = set()
    for q in [
        state.get("next_query"),
        *state.get("sop_extra_queries", []),
        *plan_queries,
    ]:
        q = (q or "").strip()
        if q and q not in seen_q:
            seen_q.add(q)
            queries.append(q)
    # the assembled set, recorded once: the pipeline submits its first six, and
    # a later gap-round query must not rewrite what the plan assembled
    if "queries" not in state:
        state["queries"] = queries[:6]

    result = retrieve_evidence(
        state["query"],
        queries,
        needs=state.get("evidence_needs", []),
        routes=state.get("routes", []),
        wants_examples=state.get("wants_examples", False),
        acc=state.get("acc", {}),
        expanded_concepts=state.get("expanded_concepts", []),
        prior_graph_evidence=state.get("graph_evidence", []),
        # a chart question's evidence is the image; concept expansion only
        # pads the prompt with taxonomy the picture already answers
        expand_graph=(
            state.get("expand_graph", True) and state.get("intent") != "chart_analysis"
        ),
    )
    state["acc"] = result.acc
    # a non-chart picture must not pull a textbook essay: one unit frames the
    # visible text. The read comes from the read node; without one (text
    # question, failed read) retrieval is untouched.
    if state.get("intent") == "chart_analysis" and is_non_chart(state.get("chart_read")):
        result.citable_units = result.citable_units[:1]
        result.citation_index = citation_binding(result.citable_units)
    state["hits"] = result.text_hits
    state["candidate_hits"] = result.candidate_hits
    state["figures"] = result.figures
    state["context_units"] = result.context_units
    state["citable_units"] = result.citable_units
    state["citation_index"] = result.citation_index
    state["toc_context"] = result.toc_context
    state["barrefs"] = result.barrefs
    state["graph_evidence"] = result.graph_evidence
    state["graph_stats"] = result.graph_stats
    state["expanded_concepts"] = result.expanded_concepts
    state.setdefault("failures", []).extend(result.failures)
    state["round"] = state.get("round", 0) + 1
    return state


def _check_node(state: QState) -> QState:
    needs = list(state.get("evidence_needs", []))
    for req in state.get("sop_evidence_requirements", []):
        if req not in needs:
            needs.append(req)
    suff, judge_view = check_sufficiency(
        state["query"],
        needs[:8],
        state.get("candidate_hits") or state.get("hits", []),
        round_=state.get("round", 0),
        max_rounds=state.get("max_rounds", 3),
    )
    state["sufficiency"] = suff.model_dump()
    state["next_query"] = suff.next_query
    # keep the last view the judge actually read: a capped round sends nothing
    # and must not overwrite the scale with an empty one
    if judge_view is not None:
        state["judge_stats"] = judge_view
    return state


def _after_check(state: QState) -> str:
    suff = state.get("sufficiency", {})
    if suff.get("sufficient") or state.get("round", 0) >= state.get("max_rounds", 3):
        return "generate"
    return "retrieve"


def _generate_node(state: QState) -> QState:
    intent = state.get("intent")
    # a chart question is answered from the image first; the book supplies the
    # criteria, not the answer (ADR-0002 keeps the read uncitable either way)
    if intent == "chart_analysis":
        mode = "chart"
    else:
        mode = "synthesis" if state.get("is_framework") else "extractive"
    # graph expansion stays a separate understanding aid: it is rendered in its
    # own prompt block and never joins the citable units / cited indices
    units = state.get("citable_units")
    if units is None:
        units = citable_units(state.get("hits", []), state.get("context_units", []))
    state["answer"] = generate(
        state["query"],
        units,
        state.get("images"),
        mode=mode,
        graph_evidence=state.get("graph_evidence"),
        reading_context=[
            u for u in state.get("context_units", []) if u.get("is_parent")
        ],
        citation_index=state.get("citation_index"),
        toc_context=state.get("toc_context"),
        fallback=state.get("sop_fallback"),
        barrefs=state.get("barrefs"),
        wants_examples=state.get("wants_examples", False),
        figure_catalog=[
            {"figure_id": f.get("figure_id"), "figure_label": f.get("figure_label")}
            for f in state.get("figures", [])
        ],
        chart_read=state.get("chart_read"),
    )
    # a degraded answer still ends the run; its classification belongs in the
    # trace (the answer dict itself stays in ``output`` with the marker)
    degradation = (state["answer"] or {}).get("degradation")
    if degradation:
        state.setdefault("failures", []).append(
            f"generate: {degradation['reason']}: {degradation['detail']}"
        )
    return state


def _record_node(state: QState) -> QState:
    ans = state.get("answer") or {}
    ev_ids = ans.get("evidence_ids") or []
    # the run trace carries the verdict and the size of the view it judged
    stats = state.get("judge_stats") or {}
    trace_output = {
        **ans,
        "sufficiency": {
            **state.get("sufficiency", {}),
            "evidence_items": stats.get("evidence_items", 0),
            "evidence_chars": stats.get("evidence_chars", 0),
        },
    }
    save_trace(
        state["run_id"],
        state["query"],
        intents=[state.get("intent", "knowledge_qa")],
        routes=state.get("routes", []),
        evidence_ids=ev_ids,
        answer=trace_output,
        failures=state.get("failures", []),
        elapsed_ms=int((time.time() - state["t0"]) * 1000),
        sop_id=state.get("sop_id"),
        plan_source=state.get("plan_source", "routed"),
        query_hash=query_hash(state.get("queries", [])),
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
    g.add_node("read", _read_node)
    g.add_node("retrieve", _retrieve_node)
    g.add_node("check", _check_node)
    g.add_node("generate", _generate_node)
    g.add_node("record", _record_node)
    g.add_edge(START, "route")
    # exactly one read per run: gap rounds loop retrieve->check, never through read
    g.add_edge("route", "read")
    g.add_edge("read", "retrieve")
    g.add_edge("retrieve", "check")
    g.add_conditional_edges("check", _after_check, {"generate": "generate", "retrieve": "retrieve"})
    g.add_edge("generate", "record")
    g.add_edge("record", END)
    return g.compile()


_graph = build_graph()


def run_query(
    query: str,
    images: list[bytes] | None = None,
    *,
    expand_graph: bool = True,
    plan: QueryPlan | None = None,
) -> dict[str, Any]:
    """Run the full production chain for one stateless question.

    ``expand_graph=False`` is the text arm of the answer-level evaluation: the
    flag reaches :func:`paknow.retrieval.pipeline.retrieve_evidence` unchanged
    and every other parameter stays identical to the graph arm.

    ``plan`` is the frozen-query seam: given a plan, the route node derives the
    same state from it and skips the router model, so two runs over the same
    questions compare code, not routing luck. Every other step is unchanged.
    """
    state = initial_state(query, images=images, expand_graph=expand_graph, plan=plan)
    final = _graph.invoke(state)
    ans = final.get("answer") or {}
    # only figures the answer actually chose to show (analysis-first, not a gallery)
    cited_figs = set(ans.get("figure_ids") or [])
    figures = [f for f in final.get("figures", []) if f.get("figure_id") in cited_figs]
    context_units = final.get("context_units", [])
    return {
        "run_id": final["run_id"],
        "plan_source": final.get("plan_source", "routed"),
        "queries": list(final.get("queries") or []),
        "query_hash": query_hash(final.get("queries") or []),
        "intent": final.get("intent"),
        "is_framework": final.get("is_framework", False),
        "routes": final.get("routes", []),
        "wants_examples": final.get("wants_examples", False),
        "hits": final.get("hits", []),
        "graph_evidence": final.get("graph_evidence", []),
        "graph_stats": final.get("graph_stats", {}),
        "figures": figures,
        "barrefs": final.get("barrefs", []),
        "answer": final.get("answer"),
        "chart_read": ans.get("chart_read"),
        "chart_read_status": ans.get("chart_read_status", "unstructured"),
        # exactly the evidence ids generation rendered as citable [n] units;
        # derived observations never enter the citation binding
        "citable_ids": list(final.get("citation_index", {})),
        # diagnostics for the optional /query debug block: the verdict the judge
        # returned, and how the context expansion was partitioned
        "sufficiency": final.get("sufficiency", {}),
        "context_unit_counts": {
            "total": len(context_units),
            "parents": sum(1 for u in context_units if u.get("is_parent")),
            "adjacent": sum(1 for u in context_units if u.get("is_adjacent")),
        },
        "rounds": final.get("round", 0),
        "failures": final.get("failures", []),
    }
