"""An injected query plan is the evaluation seam: same chain, no router model.

The frozen plan and a routed plan with the same content must reach every
downstream node with identical inputs, and the assembled query list must be
readable, capped and stable across runs.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from paknow.retrieval.pipeline import RetrievalResult
from paknow.workflows import graph as graph_module
from paknow.workflows import router as router_module
from paknow.workflows.router import QueryPlan

SOP = {
    "sop_id": "sop-1",
    "status": "active",
    "steps": [{"operation": "expand_terms"}, {"operation": "check_evidence"}],
    "required_evidence": ["comparison"],
}


def _plan(**overrides: Any) -> QueryPlan:
    plan = QueryPlan(
        intent="synthesis",
        is_framework=True,
        step_back="趋势生命周期",
        sub_queries=["子问题一", "子问题二"],
        hyde="假设性答案",
        evidence_needs=["definition", "context"],
        routes=["hybrid_text"],
        max_search_rounds=2,
    )
    for key, value in overrides.items():
        setattr(plan, key, value)
    return plan


class Chain:
    """Every model/DB step of the chain replaced by a recorder."""

    def __init__(self) -> None:
        self.retrievals: list[dict[str, Any]] = []
        self.checks: list[dict[str, Any]] = []
        self.generations: list[dict[str, Any]] = []
        self.traces: list[dict[str, Any]] = []


def _stub_chain(monkeypatch: pytest.MonkeyPatch) -> Chain:
    chain = Chain()

    def fake_retrieve(query: str, queries: list[str], **kwargs: Any) -> RetrievalResult:
        chain.retrievals.append({"query": query, "queries": list(queries), **kwargs})
        return RetrievalResult(
            text_hits=[{"evidence_id": "e1", "text": "证据", "score": 1.0}],
            citable_units=[{"evidence_id": "e1", "text": "证据", "provenance": "text"}],
            citation_index={"e1": 0},
            graph_stats={"enabled": kwargs.get("expand_graph", True)},
        )

    def fake_check(query: str, needs: list[str], hits: list[Any], **kwargs: Any) -> Any:
        chain.checks.append({"query": query, "needs": list(needs), **kwargs})
        verdict = SimpleNamespace(
            next_query=None, model_dump=lambda: {"sufficient": True, "next_query": None}
        )
        return verdict, None

    def fake_generate(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        chain.generations.append(kwargs)
        return {"answer": "答", "evidence_ids": ["e1"], "figure_ids": []}

    def fake_trace(_run_id: str, _query: str, **kwargs: Any) -> None:
        chain.traces.append(kwargs)

    monkeypatch.setattr(graph_module, "retrieve_evidence", fake_retrieve)
    monkeypatch.setattr(graph_module, "check_sufficiency", fake_check)
    monkeypatch.setattr(graph_module, "generate", fake_generate)
    monkeypatch.setattr(graph_module, "save_trace", fake_trace)
    monkeypatch.setattr(graph_module, "_maybe_auto_optimize", lambda: None)
    monkeypatch.setattr(graph_module, "select_sop", lambda intent: SOP)
    return chain


class FakeGateway:
    """Answers the router call with a fixed plan; no cloud call."""

    def __init__(self, plan: QueryPlan) -> None:
        self.plan = plan
        self.calls = 0

    def chat_json(self, _schema: Any, _messages: Any, **_kwargs: Any) -> QueryPlan:
        self.calls += 1
        return self.plan.model_copy(deep=True)


def _boom(*_args: Any, **_kwargs: Any) -> None:
    raise AssertionError("the router model must not be called for a frozen plan")


def test_an_injected_plan_skips_the_router_model(monkeypatch: pytest.MonkeyPatch) -> None:
    chain = _stub_chain(monkeypatch)
    monkeypatch.setattr(graph_module, "route", _boom)

    out = graph_module.run_query("市场周期如何转换？", plan=_plan())

    assert out["plan_source"] == "frozen"
    assert chain.retrievals  # the rest of the chain still ran
    assert chain.traces[0]["plan_source"] == "frozen"
    assert out["intent"] == "synthesis"
    assert out["is_framework"] is True
    assert out["routes"] == ["hybrid_text"]


def test_injected_and_routed_plans_reach_the_same_downstream_nodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain = _stub_chain(monkeypatch)
    monkeypatch.setattr(router_module, "gateway", FakeGateway(_plan()))

    routed = graph_module.run_query("市场周期如何转换？")
    frozen = graph_module.run_query("市场周期如何转换？", plan=_plan())

    assert [r["queries"] for r in chain.retrievals[0:2]] == [routed["queries"]] * 2
    assert chain.retrievals[0] == chain.retrievals[1]  # every node input matches
    assert chain.checks[0] == chain.checks[1]  # SOP evidence requirements included
    assert chain.generations[0] == chain.generations[1]
    assert routed["plan_source"] == "routed"
    assert {
        key: routed[key] for key in ("intent", "routes", "queries", "query_hash", "rounds")
    } == {
        key: frozen[key] for key in ("intent", "routes", "queries", "query_hash", "rounds")
    }
    assert chain.traces[0]["query_hash"] == chain.traces[1]["query_hash"]


def test_the_assembled_queries_keep_the_six_slot_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain = _stub_chain(monkeypatch)
    monkeypatch.setattr(graph_module, "select_sop", lambda intent: None)
    plan = _plan(sub_queries=[f"子问题{i}" for i in range(8)])

    out = graph_module.run_query("市场周期如何转换？", plan=plan)

    # the pipeline submits the first six of the list it is handed
    assert out["queries"] == chain.retrievals[0]["queries"][:6]
    assert out["queries"] == [
        "市场周期如何转换？",
        "趋势生命周期",
        "子问题0",
        "子问题1",
        "子问题2",
        "子问题3",
    ]


def test_the_query_list_and_its_hash_are_readable_from_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_chain(monkeypatch)
    monkeypatch.setattr(graph_module, "select_sop", lambda intent: None)

    out = graph_module.run_query("市场周期如何转换？", plan=_plan())

    assert out["queries"] == [
        "市场周期如何转换？",
        "趋势生命周期",
        "子问题一",
        "子问题二",
        "假设性答案",
    ]
    assert out["query_hash"] == graph_module.query_hash(out["queries"])
    assert out["query_hash"] != graph_module.query_hash(list(reversed(out["queries"])))


def test_the_trace_request_carries_the_plan_source_and_query_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain = _stub_chain(monkeypatch)

    out = graph_module.run_query("市场周期如何转换？", plan=_plan())

    trace = chain.traces[0]
    assert trace["plan_source"] == "frozen"
    assert trace["query_hash"] == out["query_hash"]


def test_the_same_input_twice_assembles_the_same_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain = _stub_chain(monkeypatch)
    monkeypatch.setattr(graph_module, "route", _boom)

    first = graph_module.run_query("市场周期如何转换？", plan=_plan())
    second = graph_module.run_query("市场周期如何转换？", plan=_plan())

    assert first["queries"] == second["queries"]
    assert first["query_hash"] == second["query_hash"]
    assert chain.retrievals[0]["queries"] == chain.retrievals[1]["queries"]


def test_without_a_plan_the_router_still_produces_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain = _stub_chain(monkeypatch)
    fake = FakeGateway(_plan())
    monkeypatch.setattr(router_module, "gateway", fake)

    out = graph_module.run_query("市场周期如何转换？")

    assert fake.calls == 1
    assert out["plan_source"] == "routed"
    assert chain.traces[0]["plan_source"] == "routed"
