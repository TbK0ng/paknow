"""The sufficiency judge reads the real evidence pool: id + locator per item,
bounded to 20 items x 400 chars, and the run trace records that actual scale.

Every model call is a stub: no database, no Qdrant, no cloud call.
"""
from __future__ import annotations

import time

from paknow.workflows import evidence, graph as graph_module


class FakeJudge:
    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.calls: list[list[dict[str, str]]] = []

    def chat_json(self, schema, messages, **kwargs):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return schema(sufficient=False, missing=["缺口"], next_query="补检索")


def _hit(i: int, *, text: str = "证据文本") -> dict:
    return {
        "evidence_id": f"e{i}",
        "doc_id": f"d{i}",
        "text": text,
        "chapter_path": ["第一章", f"第{i}节"],
        "pdf_page": i + 1,
        "score": 1.0 - i / 100,
    }


def _user_content(call: list[dict[str, str]]) -> str:
    return next(m["content"] for m in call if m["role"] == "user")


def _evidence_lines(user: str) -> list[str]:
    return [line for line in user.splitlines() if line.startswith("[")]


def _state(**overrides) -> dict:
    state = {
        "run_id": "r1",
        "query": "问题",
        "t0": time.time(),
        "round": 1,
        "max_rounds": 3,
        "evidence_needs": ["定义"],
        "failures": [],
        "answer": {"answer": "答", "evidence_ids": ["e0"]},
    }
    state.update(overrides)
    return state


# --- the judge view -------------------------------------------------------- #


def test_forty_hits_send_twenty_items_capped_at_four_hundred_chars(monkeypatch):
    judge = FakeJudge()
    monkeypatch.setattr(evidence, "gateway", judge)
    hits = [_hit(i, text="长" * 900) for i in range(40)]

    suff, sent = evidence.check_sufficiency(
        "问题", ["定义"], hits, round_=1, max_rounds=3
    )

    user = _user_content(judge.calls[0])
    lines = _evidence_lines(user)
    assert len(lines) == 20
    # each item carries its evidence id and a readable locator, in rank order
    assert [line.split(" | ")[0] for line in lines] == [
        f"[{i}] 证据 e{i - 1}" for i in range(1, 21)
    ]
    assert "第一章 › 第0节 · PDF第1页" in lines[0]
    assert all(len(line.split(" | ", 2)[2]) == 400 for line in lines)
    # the audited scale is the block the judge actually received
    block = "\n".join(lines)
    assert block in user
    assert sent == {"round": 1, "evidence_items": 20, "evidence_chars": len(block)}
    assert suff.sufficient is False
    assert suff.missing == ["缺口"]
    assert suff.next_query == "补检索"


def test_multiline_text_stays_one_item_line(monkeypatch):
    judge = FakeJudge()
    monkeypatch.setattr(evidence, "gateway", judge)

    evidence.check_sufficiency(
        "问题", ["定义"], [_hit(0, text="第一行\n第二行\t第三行")], round_=1, max_rounds=3
    )

    user = _user_content(judge.calls[0])
    assert len(_evidence_lines(user)) == 1
    assert "第一行 第二行 第三行" in user


def test_round_cap_sends_no_prompt_and_reports_no_view(monkeypatch):
    judge = FakeJudge()
    monkeypatch.setattr(evidence, "gateway", judge)

    suff, sent = evidence.check_sufficiency(
        "问题", ["定义"], [_hit(0)], round_=3, max_rounds=3
    )

    assert suff.sufficient is True
    assert sent is None
    assert judge.calls == []


def test_zero_hits_fail_open_without_a_prompt(monkeypatch):
    judge = FakeJudge()
    monkeypatch.setattr(evidence, "gateway", judge)

    suff, sent = evidence.check_sufficiency("问题", [], [], round_=1, max_rounds=3)

    assert suff.sufficient is False
    assert suff.missing == ["无检索结果"]
    assert sent is None
    assert judge.calls == []


def test_judge_failure_fails_open_and_still_reports_the_view(monkeypatch):
    monkeypatch.setattr(evidence, "gateway", FakeJudge(error=RuntimeError("boom")))

    suff, sent = evidence.check_sufficiency(
        "问题", ["定义"], [_hit(i) for i in range(3)], round_=2, max_rounds=3
    )

    assert suff.sufficient is True
    assert sent is not None
    assert sent["round"] == 2
    assert sent["evidence_items"] == 3


# --- what the graph node hands over and records ---------------------------- #


def test_check_node_judges_the_candidate_pool_not_the_diversified_hits(monkeypatch):
    judge = FakeJudge()
    monkeypatch.setattr(evidence, "gateway", judge)
    state = _state(
        hits=[_hit(i) for i in range(14)],
        candidate_hits=[_hit(i) for i in range(40)],
    )

    graph_module._check_node(state)

    user = _user_content(judge.calls[0])
    lines = _evidence_lines(user)
    assert len(lines) == 20
    assert "证据 e19" in lines[-1]
    assert state["judge_stats"]["evidence_items"] == 20


def test_run_trace_records_the_judge_view_scale(monkeypatch):
    judge = FakeJudge()
    monkeypatch.setattr(evidence, "gateway", judge)
    saved: dict = {}

    def fake_save(run_id, query, **kwargs):
        saved.update(kwargs)

    monkeypatch.setattr(graph_module, "save_trace", fake_save)
    monkeypatch.setattr(graph_module, "_maybe_auto_optimize", lambda: None)
    state = _state(candidate_hits=[_hit(i) for i in range(40)])

    graph_module._check_node(state)
    graph_module._record_node(state)

    user = _user_content(judge.calls[0])
    block = "\n".join(_evidence_lines(user))
    sufficiency = saved["answer"]["sufficiency"]
    assert sufficiency["evidence_items"] == 20
    assert sufficiency["evidence_chars"] == len(block)
    # the verdict travels with the scale, the answer keys are untouched
    assert sufficiency["sufficient"] is False
    assert saved["answer"]["answer"] == "答"
    assert saved["answer"]["evidence_ids"] == ["e0"]


def test_capped_round_keeps_the_last_view_that_was_actually_judged(monkeypatch):
    monkeypatch.setattr(graph_module, "save_trace", lambda *a, **k: None)
    monkeypatch.setattr(graph_module, "_maybe_auto_optimize", lambda: None)
    state = _state(candidate_hits=[_hit(i) for i in range(40)])

    monkeypatch.setattr(evidence, "gateway", FakeJudge())
    graph_module._check_node(state)
    judged = dict(state["judge_stats"])

    # round 3 hits the cap: no evidence is sent, so the trace keeps round 2's view
    state["round"] = 3
    graph_module._check_node(state)

    assert state["judge_stats"] == judged
    assert state["sufficiency"]["sufficient"] is True
