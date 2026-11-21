"""Role separation, same-passage dedupe and graph-only fallback for the PA packet."""
from hashlib import sha256

import pytest

from paknow.graph.pa_context import Lead, ROLES, Setup
from paknow.retrieval import pa_packet


def source(eid, family, text):
    return {"evidence_id": eid, "passage_id": family, "text": text, "doc_id": "fresh",
            "doc_title": "Text", "provenance": "native_text", "locator": {}, "block_type": "paragraph",
            "text_hash": sha256(text.encode()).hexdigest()}


def lead(role, row, profile="any"):
    return Lead(role, "second_entry", f"question {role}", row["evidence_id"], row["text_hash"], "REQUIRES_CONTEXT", profile)


def setup():
    return Setup("long", "range_or_mixed", "mixed", "upper", "ema_proxy", "same_side_close",
                 100.0, 1, False, False, False, (), False)


def test_independent_roles_win_over_text_score_and_same_parent_cannot_supply_risk():
    shared = source("a", "family", "支持理由：真实的价格趋势回调和第二次测试需要原文条件。")
    sibling = source("b", "family", "反对理由：同一段落的例外部分不算另一条独立证据。")
    confirmation = source("c", "other", "确认：信号棒与后续的跟随棒属于两个不同时间点。")
    text_only = source("d", "unverified", "仅仅相似的未核对文本。")
    ranked = {"support": [text_only, shared], "risk": [sibling, text_only], "confirmation": [confirmation]}
    verified = {"support": {"a": lead("support", shared)}, "risk": {"b": lead("risk", sibling)},
                "confirmation": {"c": lead("confirmation", confirmation)}}
    result, gaps = pa_packet.select_sources(ranked, verified)
    assert [(r["id"], r["role"], r["verified"]) for r in result] == [
        ("a", "support", True), ("c", "confirmation", True), ("d", "support", False)]
    assert gaps == ["risk"]


def test_graph_fallback_preserves_text_but_does_not_invent_a_verified_role(monkeypatch):
    def graph_down(_setup):
        raise OSError("graph unavailable")
    text = source("a", "unique", "技术分析原文：价格在交易区间的边缘可能发生失败突破，需要观察随后同方向的独立跟进。")
    monkeypatch.setattr(pa_packet, "corpus", lambda: ("fresh", frozenset({"fresh"})))
    monkeypatch.setattr(pa_packet.pa_context, "plan", graph_down)
    monkeypatch.setattr(pa_packet, "hybrid_search", lambda _q, **_kw: [1])
    monkeypatch.setattr(pa_packet, "point_to_doc", lambda _p: {"evidence_id": "a"})
    monkeypatch.setattr(pa_packet, "citation_ready_hits", lambda _rows, **_kw: [text])
    packet = pa_packet.retrieve_packet(setup())
    assert packet.graph_status == "unavailable" and not packet.failures
    assert packet.gaps == list(ROLES)
    assert len(packet.sources) == 1 and not packet.sources[0]["verified"]


def test_graph_and_text_share_rerank_and_only_exact_hashes_verify(monkeypatch):
    original = source("g", "g", "符合图谱锚点的逐字原文，保留支持条件、背景限制与文本出处，需要逐段核对。")
    text = source("t", "p2", "另一个文本检索结果；即使得分更高，也不能从搜索排名推断它一定适用于候选。")
    broken = Lead("risk", "second_entry", "风险问题", "g", "wrong", "EXCEPTION_TO", "any")
    monkeypatch.setattr(pa_packet, "corpus", lambda: ("fresh", frozenset({"fresh"})))
    monkeypatch.setattr(pa_packet.pa_context, "plan", lambda _setup: (lead("support", original), broken))
    monkeypatch.setattr(pa_packet, "hybrid_search", lambda _q, **_kw: [1])
    monkeypatch.setattr(pa_packet, "point_to_doc", lambda _p: {"evidence_id": "t"})
    by_id = {r["evidence_id"]: r for r in (original, text)}
    monkeypatch.setattr(pa_packet, "citation_ready_hits", lambda rows, **_kw: [by_id[r["evidence_id"]] for r in rows])
    seen = []
    monkeypatch.setattr(pa_packet, "rerank_best", lambda rows, by_query, *, fallback_query:
                        seen.append((fallback_query, {r["evidence_id"] for r in rows}, by_query)) or rows[::-1])
    packet = pa_packet.retrieve_packet(setup())
    assert seen[0][:2] == ("question support", {"g", "t"})
    assert seen[0][2]["g"] == ["question support"]
    assert len(seen[0][2]["t"]) == 2
    assert packet.gaps == ["risk", "confirmation"]
    assert packet.sources[0]["id"] == "g" and packet.sources[0]["verified"]


def test_role_assignment_tries_second_support_before_giving_up_only_risk():
    s1 = source("s1", "shared", "First support.")
    s2 = source("s2", "second", "Independent alternative support.")
    r = source("r", "shared", "The only checked counterexample.")
    c = source("c", "third", "A confirmed close is a separate event.")
    ranked = {"support": [s1, s2], "risk": [r], "confirmation": [c]}
    verified = {role: {row["evidence_id"]: lead(role, row) for row in rows} for role, rows in ranked.items()}
    result, gaps = pa_packet.select_sources(ranked, verified)
    assert gaps == [] and [row["id"] for row in result] == ["s2", "r", "c"]


def test_all_planned_questions_are_used_and_optional_recall_does_not_discard_checked_pack(monkeypatch):
    rows = {role: source(role, role, f"An original checked passage for {role} with a clear condition.") for role in ROLES}
    extra = source("risk2", "risk2", "Another independently checked risk tied to a different setup condition.")
    second_risk = Lead("risk", "second_entry", "second risk question", "risk2", extra["text_hash"], "EXCEPTION_TO", "proxy")
    obstacle = source("risk3", "risk3", "A distinct checked obstacle may prevent continuation toward the target.")
    third_risk = Lead("risk", "second_entry", "third risk question", "risk3", obstacle["text_hash"], "EXCEPTION_TO", "obstacle")
    leads = tuple(lead(role, rows[role]) for role in ROLES) + (second_risk, third_risk)
    calls = []

    def recall(query, **kwargs):
        calls.append(query)
        if query == second_risk.question:
            raise RuntimeError("unavailable secondary recall")
        return []

    monkeypatch.setattr(pa_packet, "corpus", lambda: ("fresh", frozenset({"fresh"})))
    monkeypatch.setattr(pa_packet.pa_context, "plan", lambda _setup: leads)
    monkeypatch.setattr(pa_packet, "hybrid_search", recall)
    by_id = {**rows, "risk2": extra, "risk3": obstacle}
    monkeypatch.setattr(pa_packet, "citation_ready_hits", lambda rs, **_kw: [by_id[r["evidence_id"]] for r in rs])
    monkeypatch.setattr(pa_packet, "rerank_best", lambda rs, *_a, **_kw: rs[::-1])
    packet = pa_packet.retrieve_packet(setup())
    assert not packet.failures and not packet.gaps and packet.warnings == ["risk:retrieve:RuntimeError"]
    assert second_risk.question in calls and third_risk.question in calls
    assert packet.query_plan["risk"][1:3] == (second_risk.question, third_risk.question)
    assert [(row["id"], row["verified"]) for row in packet.sources if row["role"] == "risk"] == [
        ("risk", True), ("risk2", True), ("risk3", True)]


@pytest.mark.parametrize("parent_problem", ["missing_parent", "invalid_parent_span"])
def test_hash_verified_roles_still_need_valid_reading_context(monkeypatch, parent_problem):
    rows = {role: source(role, role, f"A checked original passage with the reason for {role}.") for role in ROLES}
    child = rows["support"]
    child.update(passage_id="parent", locator={"doc_id": "fresh", "span_start": 0})
    parent = source("parent", "parent", "Another unrelated source paragraph, not the quoted child.")
    if parent_problem != "missing_parent":
        rows["parent"] = parent
    monkeypatch.setattr(pa_packet, "corpus", lambda: ("fresh", frozenset({"fresh"})))
    monkeypatch.setattr(pa_packet.pa_context, "plan", lambda _setup: tuple(lead(role, rows[role]) for role in ROLES))
    monkeypatch.setattr(pa_packet, "hybrid_search", lambda *a, **kw: [])
    monkeypatch.setattr(pa_packet, "citation_ready_hits", lambda hits, **kw:
                        [rows[h["evidence_id"]] for h in hits if h["evidence_id"] in rows])
    packet = pa_packet.retrieve_packet(setup())
    assert not packet.gaps and all(row["verified"] for row in packet.sources)
    assert packet.failures == [f"support:{parent_problem}"]
