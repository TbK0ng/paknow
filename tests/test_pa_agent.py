"""PA Agent rules, real-only feed and grounded read-only response contracts."""
from __future__ import annotations

import json
from dataclasses import replace
from math import sin
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api import pa_agent
from paknow import pa_analysis
from paknow.pa_analysis import Commentary, Point
from paknow.pa_background import LOOKBACK, describe, summarize
from paknow.pa_market import BARS, MIN_WARMUP, WARMUP, assemble, download
from paknow.pa_rules import HOUR, STEP, Bar
from paknow.pa_swing import screen

START = 1_735_689_600_000


def tape(length: int) -> list[Bar]:
    bars = []
    for i in range(length):
        close = 1000 + i * 0.2 + 15 * sin(i / 8)
        opening = bars[-1].close if bars else 1000
        bars.append(Bar(START + STEP * i, opening, max(opening, close) + 0.8, min(opening, close) - 0.7, close, 30))
    return bars


@pytest.fixture(scope="module")
def market():
    bars = tape(BARS + WARMUP + 4)
    return assemble(bars, bars[-1].ts_open + STEP + 100)


def test_public_window_excludes_forming_candles_and_has_no_gaps():
    bars = tape(BARS + WARMUP + 9)
    latest_open = bars[-1].ts_open
    seen = []

    def okx(request: httpx.Request):
        assert request.url.host == "www.okx.com" and request.url.path.endswith("/history-candles")
        assert request.url.params["instId"] == "BTC-USDT-SWAP"
        assert not request.headers.get("OK-ACCESS-KEY")
        after = int(request.url.params["after"])
        seen.append(after)
        page = [bar for bar in reversed(bars) if bar.ts_open < after or (len(seen) == 1 and bar.ts_open == latest_open)][:100]
        return httpx.Response(200, json={"code": "0", "data": [
            [str(bar.ts_open), str(bar.open), str(bar.high), str(bar.low), str(bar.close), str(bar.volume), "0", "0", "0" if bar.ts_open == latest_open else "1"]
            for bar in page
        ]})

    with httpx.Client(transport=httpx.MockTransport(okx)) as client:
        m = download(client, latest_open + 1)
    assert len(seen) > 1 and all(a > b for a, b in zip(seen, seen[1:]))
    assert len(m.bars) == BARS and m.last_ms == latest_open
    assert m.last_ms - m.bars[0].ts_open == 120 * HOUR
    broken = list(m.tape)
    broken.pop(len(broken) - 4)
    with pytest.raises(ValueError):
        assemble(broken, m.fetched_ms)


def test_background_distinguishes_direction_and_range():
    def series(values):
        return tuple(Bar(START + i * STEP, p, p + 1, p - 1, p, 10) for i, p in enumerate(values))

    assert summarize(series(range(100, 180))).regime == "up"
    assert summarize(series(range(180, 100, -1))).regime == "down"
    assert summarize(series([100, 103] * 40)).regime == "range_or_mixed"
    with pytest.raises(ValueError):
        summarize(series(range(100, 179)))


def test_old_real_snapshot_still_loads_without_inventing_4h_history():
    bars = tape(BARS + MIN_WARMUP + 4)
    market = assemble(bars, bars[-1].ts_open + STEP + 100)
    assert len(market.tape) == BARS + MIN_WARMUP + 4
    assert market.states[0]["decision"]["timeframes"]["major"]["available"] is False
    assert market.states[-1]["decision"]["timeframes"]["major"]["available"] is True


def test_rule_and_context_use_the_same_closed_cutoff(market):
    close_ms = market.last_ms - 8 * STEP
    rule, ctx = market.at(close_ms)
    assert ctx[-1].ts_open + STEP == close_ms and rule["higher_close_ms"] <= close_ms
    i = next(i for i, b in enumerate(market.tape) if b.ts_open + STEP == close_ms)
    from paknow.pa_rules import diagnose, hourly
    prefix = market.tape[:i + 1]
    base = diagnose(prefix, hourly(prefix))
    context = describe(summarize(prefix[-LOOKBACK - 1:-1]), base)
    swing = screen(base)
    assert rule["original_state"] == base["state"] and rule["swing"] == swing
    assert rule["background"] == context["background"] and rule["plan"] == context["plan"]
    assert rule["state"] == ("wait" if swing["status"] == "blocked" else base["state"])
    assert len(ctx) == LOOKBACK + 1
    assert rule["background"]["end_ms"] == ctx[-1].ts_open
    assert rule["background"]["start_ms"] == ctx[0].ts_open
    decision = rule["decision"]
    assert decision["session"] == "continuous_24_7" and decision["gap_analysis"] == "not_applicable"
    assert decision["timeframes"]["execution"]["label"] == "15m"
    assert decision["timeframes"]["middle"]["label"] == "1h"
    assert decision["timeframes"]["major"]["label"] == "4h"
    assert decision["timeframes"]["major"]["close_ms"] <= close_ms
    assert decision["follow_through"]["status"] == "awaiting_next_closed_bar"
    # Future bars can change hits or exits, but cannot change this plan.
    source = list(market.tape)
    source[i + 1] = replace(source[i + 1], high=source[i + 1].high + 100)
    assert assemble(source, market.fetched_ms).at(close_ms) == (rule, ctx)
    early, first_ctx = market.at(market.bars[0].ts_open + STEP)
    assert early["background"]["count"] == LOOKBACK
    assert early["decision"]["timeframes"]["major"]["available"] is True
    assert first_ctx[:-1] == market.tape[-BARS - LOOKBACK:-BARS]
    assert early["background"]["high"] == max(bar.high for bar in first_ctx[:-1])
    assert len(rule["short"]["checks"]) == len(rule["long"]["checks"]) == 5
    assert {row["original_state"] for row in market.states} >= {"wait", "long"}
    with pytest.raises(LookupError):
        market.at(close_ms + 1)


def test_background_changes_review_input_but_not_first_layer(market, monkeypatch):
    from paknow import pa_market

    def opposing_summary(bars):
        bg = summarize(bars)
        return replace(bg, regime="down" if bg.regime != "down" else "up")

    monkeypatch.setattr(pa_market, "summarize", opposing_summary)
    alternative = assemble(list(market.tape), market.fetched_ms)
    assert all(a["swing"] == b["swing"] for a, b in zip(market.states, alternative.states, strict=True))
    index = next(i for i, key in enumerate(market.review_keys) if key)
    assert market.review_keys[index] != alternative.review_keys[index]
    from paknow.graph.pa_context import base_questions, setup_at

    close_ms = market.bars[index].ts_open + STEP
    assert base_questions(setup_at(*market.at(close_ms))) != base_questions(setup_at(*alternative.at(close_ms)))


def test_grounding_uses_wait_checks_and_model_cannot_change_the_decision(market, monkeypatch):
    i = next(i for i, r in enumerate(market.states) if r["swing"]["status"] == "no_trigger")
    rule, context = market.at(market.bars[i].ts_open + STEP)
    captured = []
    hit = {"id": "source-1", "text": "原文", "label": "教材", "provenance": "native_text"}
    monkeypatch.setattr(pa_analysis, "_lookup", lambda qs: captured.extend(qs) or [hit])
    monkeypatch.setattr(pa_analysis, "settings", SimpleNamespace(has_cloud=True))
    points = Point(text="这是根据给出的证据解释等待的原因。", evidence_ids=["invented"])
    monkeypatch.setattr(pa_analysis.gateway, "chat_json", lambda *_a, **_kw: Commentary(context=points, risks=points, watch=points, review=points))
    result = pa_analysis.explain(rule, context, cloud=True)
    assert captured and any("趋势" in q or "等待" in q for q in captured)
    assert result["rule"]["state"] == "wait" and result["sources"] == [hit]
    assert result["commentary"] is None and result["commentary_status"] == "unavailable"
    assert set(result["local_commentary"]) == {"context", "risks", "watch", "review"}
    assert "多头缺" in result["local_commentary"]["risks"]["text"]
    cited = Point(text=points.text, evidence_ids=["source-1"])
    def explain_model(_schema, messages, **_kwargs):
        payload = json.loads(messages[1]["content"])
        previous = payload["previous_80_oldest_first"]
        assert len(previous) == LOOKBACK
        assert previous[-1]["ts_open"] + STEP == payload["signal_bar"]["ts_open"]
        assert payload["signal_bar"]["ts_open"] + STEP == rule["close_ms"]
        return Commentary(context=cited, risks=cited, watch=cited, review=cited)

    monkeypatch.setattr(pa_analysis.gateway, "chat_json", explain_model)
    result = pa_analysis.explain(rule, context, cloud=True)
    assert result["commentary_status"] == "ok"
    assert "信号以前 80 根" in result["local_commentary"]["context"]["text"]

    invented = cited.model_copy(update={"quote": pa_analysis.Quote(evidence_id="source-1", text="这段话没有出现在实际原文里。")})
    monkeypatch.setattr(pa_analysis.gateway, "chat_json", lambda *_a, **_kw:
                        Commentary(context=cited, risks=invented, watch=cited, review=cited))
    assert pa_analysis.explain(rule, context, cloud=True)["commentary_status"] == "unavailable"


def test_native_routes_replay_and_explain_without_rust(market, monkeypatch, tmp_path):
    from apps.api.main import app
    from paknow.pa_review import ReviewLedger
    monkeypatch.setattr(pa_agent, "review_ledger", ReviewLedger(tmp_path / "review.db"))
    monkeypatch.setattr(pa_agent.market_feed, "get", lambda **_kwargs: (market, False))
    monkeypatch.setattr(pa_agent, "explain", lambda rule, _bars, *, cloud: {"rule": rule, "sources": []})
    client = TestClient(app)
    assert "PA Agent" in client.get("/pa-agent").text
    response = client.get("/pa-agent/market").json()
    assert len(response["bars"]) == 480 and "systems" not in response
    assert response["background_count"] == len(response["prelude"]) == LOOKBACK
    assert response["trades"] == []
    assert "pending" in response["reviews"]
    close_ms = next(b["ts_open"] + STEP for b in response["bars"] if b["swing_status"] == "no_trigger")
    rule = client.get(f"/pa-agent/rule?close_ms={close_ms}").json()["rule"]
    assert client.post("/pa-agent/analyze", json={"close_ms": close_ms}).json()["rule"] == rule
    assert client.post("/pa-agent/analyze", json={"close_ms": response["last_ms"] + STEP}).status_code == 404
    assert client.post("/pa-agent/analyze", json={"close_ms": close_ms, "execute": True}).status_code == 422


def _paper_plan(rule) -> dict:
    side = rule["original_state"]
    close = rule["facts"]["close"]
    sign = 1 if side == "long" else -1
    return {"side": side, "entry_mode": "next_open", "entry_level": None, "entry_window_bars": 1,
            "stop_price": close - sign * 20, "target_price": close + sign * 40,
            "exit_on_close": None, "max_hold_bars": 6}


def test_evidence_and_citations_are_hard_requirements_for_review(market, monkeypatch):
    i = next(i for i, key in enumerate(market.review_keys) if key)
    state, history = market.at(market.bars[i].ts_open + STEP)
    from paknow.retrieval.pa_packet import Packet

    refs = [{"id": eid, "text": text, "role": role, "verified": True, "passage_id": eid}
            for eid, text, role in [("a", "顺势回调后需要观察确认条件。", "support"),
                                     ("b", "没有跟进时容易形成失败突破。", "risk"),
                                     ("c", "下一根同向收盘才算独立跟进。", "confirmation")]]
    questions = {role: role for role in ("support", "risk", "confirmation")}
    monkeypatch.setattr(pa_analysis, "settings", SimpleNamespace(has_cloud=True, pa_review_max_tokens=24_576))
    monkeypatch.setattr(pa_analysis, "retrieve_packet", lambda _setup: Packet(questions, refs[:1], ["risk"], "ok", [], "g"))
    monkeypatch.setattr(pa_analysis.gateway, "chat_json", lambda *_a, **_kw: pytest.fail("no cloud call on evidence gaps"))
    assert pa_analysis.explain(state, history, cloud=True)["commentary_status"] == "insufficient_evidence"
    monkeypatch.setattr(pa_analysis, "retrieve_packet", lambda _setup: Packet(questions, refs, [], "ok", [], "g"))

    def canned(schema, messages, *, error=None, max_tokens, timeout, attempts):
        assert max_tokens == 24_576 and timeout == 150 and attempts == 1
        payload = json.loads(messages[1]["content"])
        assert len(payload["previous_80_oldest_first"]) == 80 and "trades" not in payload
        assert all(x["ts_open"] <= history[-1].ts_open for x in payload["causal_facts"].values())
        assert payload["decision"]["gap_analysis"] == "not_applicable"
        assert payload["decision"]["timeframes"]["major"]["label"] == "4h"
        def point(src, fact, quote=None):
            return Point(text="根据候选现有结构评估原文条件与本例是否符合。", evidence_ids=[src["id"]],
                         quote={"evidence_id": src["id"], "text": quote or src["text"]}, market_refs=[fact])
        thesis = point(refs[0], "signal_close")
        risks = point(refs[0] if error == "role" else refs[1], "future" if error == "fact" else "range_high",
                      "这个句子并不存在于被引用的原文里。" if error == "quote" else None)
        watch = point(refs[2], "previous_high")
        anchors = {"stop_price": {"ts_open": history[-1].ts_open + (STEP if error == "future" else 0), "field": "low"},
                   "target_price": {"ts_open": history[-2].ts_open, "field": "high"}}
        return schema(verdict="approve", support_status="needs_closed_confirmation" if error == "trigger" else "observed",
                      thesis=thesis, risks=risks, context=thesis, review=risks, watch=watch,
                      paper_plan=_paper_plan(state), price_anchors=anchors)

    for error in ("role", "quote", "fact", "future", "trigger"):
        monkeypatch.setattr(pa_analysis.gateway, "chat_json", lambda *a, **kw: canned(*a, error=error, **kw))
        assert pa_analysis.explain(state, history, cloud=True)["commentary_status"] == "unavailable"
    monkeypatch.setattr(pa_analysis.gateway, "chat_json", canned)
    approved = pa_analysis.explain(state, history, cloud=True)
    assert approved["commentary_status"] == "ok" and approved["commentary"]["verdict"] == "approve"
    assert approved["commentary"]["price_anchors"]["stop_price"]["basis_price"] == history[-1].low
    assert approved["rule"] == state
    from paknow.gateway import ModelOutputError

    def truncated(*args, **kwargs):
        exc = ModelOutputError("", "completion was truncated")
        exc.finish_reason = "length"
        raise exc

    monkeypatch.setattr(pa_analysis.gateway, "chat_json", truncated)
    incomplete = pa_analysis.explain(state, history, cloud=True)
    assert incomplete["commentary_failure"] == "length" and incomplete["commentary"] is None
    assert incomplete["review_evidence_ready"] and incomplete["sources"] == refs


@pytest.mark.parametrize("has_cloud,problem", [(True, None), (False, None), (True, "missing_parent")])
def test_candidate_can_preview_exact_packet_without_spending_or_recording(market, monkeypatch, tmp_path, has_cloud, problem):
    from apps.api.main import app
    from paknow import pa_review
    from paknow.pa_preview import EvidencePreviews
    from paknow.retrieval.pa_packet import Packet, ROLES

    generation = "aa" * 32
    for module in (pa_agent, pa_analysis, pa_review):
        monkeypatch.setattr(module, "corpus", lambda: (generation, frozenset({"doc"})))
    market = assemble(list(market.tape), market.fetched_ms)
    index = next(i for i, key in enumerate(market.review_keys) if key)
    inputs = {"close_ms": market.bars[index].ts_open + STEP, "fingerprint": market.review_keys[index]}
    ledger = pa_review.ReviewLedger(tmp_path / "preview.db")
    monkeypatch.setattr(pa_agent, "review_ledger", ledger)
    monkeypatch.setattr(pa_agent, "_evidence_previews", EvidencePreviews())
    monkeypatch.setattr(pa_analysis, "settings", SimpleNamespace(has_cloud=has_cloud))
    monkeypatch.setattr(pa_agent.market_feed, "get", lambda **_: (market, False))
    queries = {role: ["test query " + role] for role in ROLES}
    sources = [{"id": role, "role": role, "verified": True, "label": "A source",
                "text": "A checked source", "passage_id": role} for role in ROLES]
    retrieval, dispatched = [], []

    def lookup(setup):
        retrieval.append(setup)
        return Packet({key: children[0] for key, children in queries.items()}, sources, [], "ok",
                      [f"support:{problem}"] if problem else [], generation, query_plan=queries,
                      warnings=["risk:rerank:ValueError"])

    def model(_state, _bars, hits, *, timings, entry_review):
        assert entry_review and hits == sources
        dispatched.append(hits)
        timings["model"] = 0.001
        return {"verdict": "wait", "paper_plan": None}

    monkeypatch.setattr(pa_analysis, "retrieve_packet", lookup)
    monkeypatch.setattr(pa_analysis, "_generate", model)
    client = TestClient(app)
    invalid = client.post("/pa-agent/review-evidence", json={**inputs, "fingerprint": "11" * 32})
    assert invalid.status_code == 409 and not retrieval
    reply = client.post("/pa-agent/review-evidence", json=inputs)
    shown = reply.json()
    assert reply.status_code == 200 and reply.headers["Cache-Control"] == "no-store"
    assert shown["fingerprint"] == inputs["fingerprint"] and shown["snapshot"] == list(pa_agent._snapshot(market))
    assert shown["commentary"] is None and not shown["model_called"] and not ledger.path.exists() and not dispatched
    assert shown["market_anchors"]["signal_close"]["ts_open"] == inputs["close_ms"] - STEP
    assert shown["evidence_gaps"] == [] and shown["review_evidence_ready"] is not bool(problem)
    assert shown["evidence_failures"] == ([f"support:{problem}"] if problem else [])
    expired = client.post("/pa-agent/review", json={**inputs, "evidence_token": "f" * 32})
    assert expired.status_code == 409 and expired.headers["X-PA-Review-State"] == "preview_expired"
    assert len(retrieval) == 1 and not dispatched
    if not problem and has_cloud:
        reviewed = client.post("/pa-agent/review", json={**inputs, "evidence_token": shown["evidence_token"]}).json()
        assert len(retrieval) == len(dispatched) == 1 and reviewed["sources"] == shown["sources"]
        assert reviewed["review_status"] == "wait" and reviewed["evidence_warnings"] == shown["evidence_warnings"]
    elif problem:
        assert shown["evidence_token"] is None
        blocked = client.post("/pa-agent/review", json=inputs).json()
        assert blocked["commentary_status"] == "insufficient_evidence" and not dispatched and not ledger.path.exists()
    else:
        assert shown["evidence_token"] and not dispatched


@pytest.mark.parametrize("stage", ["before_request", "after_lookup", "after_model"])
def test_candidate_review_does_not_persist_across_corpus_change(market, monkeypatch, tmp_path, stage):
    from apps.api.main import app
    from paknow import pa_review
    from paknow.retrieval.pa_packet import Packet

    generation = ["abc"]
    for module in (pa_agent, pa_analysis, pa_review):
        monkeypatch.setattr(module, "corpus", lambda: (generation[0], frozenset({"doc"})))
    market = assemble(list(market.tape), market.fetched_ms)
    index = next(i for i, key in enumerate(market.review_keys) if key)
    inputs = {"close_ms": market.bars[index].ts_open + STEP, "fingerprint": market.review_keys[index]}
    ledger = pa_review.ReviewLedger(tmp_path / "review.db")
    monkeypatch.setattr(pa_agent, "review_ledger", ledger)
    monkeypatch.setattr(pa_analysis, "settings", SimpleNamespace(has_cloud=True))
    monkeypatch.setattr(pa_agent.market_feed, "get", lambda **_: (market, False))
    called = []

    def lookup(_setup):
        old = generation[0]
        if stage == "after_lookup":
            generation[0] = "def"
        return Packet({}, [{"id": "test", "text": "Checked text"}], [], "ok", [], old)

    def model(*_args, **_kwargs):
        called.append(True)
        generation[0] = "def"
        return {"verdict": "wait", "paper_plan": None}

    monkeypatch.setattr(pa_analysis, "retrieve_packet", lookup)
    monkeypatch.setattr(pa_analysis, "_generate", model)
    if stage == "before_request":
        generation[0] = "def"
        assert TestClient(app).post("/pa-agent/review-evidence", json=inputs).status_code == 409
    assert TestClient(app).post("/pa-agent/review", json=inputs).status_code == 409
    assert bool(called) is (stage == "after_model") and not ledger.path.exists()


@pytest.mark.parametrize("verdict, expected_count", [("approve", 1), ("wait", 0), ("reject", 0), (None, 0)])
def test_review_controls_replay_and_reuses_causal_verdict(market, monkeypatch, tmp_path, verdict, expected_count):
    from apps.api.main import app
    from paknow.pa_review import ReviewLedger

    ledger = ReviewLedger(tmp_path / "replay.db")
    monkeypatch.setattr(pa_agent, "review_ledger", ledger)
    feed = [market]
    monkeypatch.setattr(pa_agent.market_feed, "get", lambda **_kwargs: (feed[0], False))
    calls = []
    index = next(i for i, key in enumerate(market.review_keys) if key)
    close = market.bars[index].ts_open + STEP

    def fake_review(rule, bars, *, cloud, timings, inputs_current):
        assert inputs_current()
        timings["lookup"] = 0.012
        assert cloud and len(bars) == 81 and bars[-1].ts_open + STEP == close
        assert "trades" not in rule and "exit_index" not in rule
        calls.append(close)
        return {"rule": rule, "retrieval_status": "ok", "commentary_status": "ok" if verdict else "unavailable",
                "commentary": {"verdict": verdict, "paper_plan": _paper_plan(rule) if verdict == "approve" else None} if verdict else None}

    monkeypatch.setattr(pa_agent, "explain", fake_review)
    client = TestClient(app)
    assert not calls and not client.get("/pa-agent/market").json()["trades"]
    selected = client.get(f"/pa-agent/rule?close_ms={close}").json()
    args = {"close_ms": close, "fingerprint": selected["fingerprint"]}
    assert selected["review_status"] == "pending" and selected["review_source"] is None and not calls
    assert client.post("/pa-agent/review", json={**args, "fingerprint": "0" * 64}).status_code == 409
    request = client.post("/pa-agent/review", json=args)
    assert "lookup;dur=12.0" in request.headers["server-timing"]
    assert all(stage + ";dur=" in request.headers["server-timing"] for stage in ("snapshot", "review", "projection"))
    response = request.json()
    assert len(response["trades"]) == expected_count
    assert response["review_status"] == (verdict or "pending")
    assert response["review_source"] == "attempt" and not response["model_called"]
    # Simulate a fresh page and a new ledger instance with the same on-disk data.
    monkeypatch.setattr(pa_agent, "review_ledger", ReviewLedger(ledger.path))
    page_state = client.get("/pa-agent/market")
    detail = client.get(f"/pa-agent/rule?close_ms={close}")
    assert page_state.headers["Cache-Control"] == detail.headers["Cache-Control"] == "no-store"
    assert page_state.json()["reviews"][index] == (verdict or "pending")
    assert detail.json()["review_source"] == ("saved" if verdict else None)
    assert len(calls) == 1
    if expected_count:
        trade = response["trades"][0]
        proposed = _paper_plan(selected["rule"])
        assert (trade["signal_index"], trade["entry_index"], trade["entry_price"], trade["stop_price"], trade["target_price"]) == (
            index, index + 1, market.bars[index + 1].open, proposed["stop_price"], proposed["target_price"])
        assert response["orders"] == [{"signal_index": index, "status": "filled", "result_index": index + 1}]
    repeated = client.post("/pa-agent/review", json=args)
    assert len(repeated.json()["trades"]) == expected_count
    assert ("lookup;dur=" in repeated.headers["server-timing"]) == (verdict is None)
    assert repeated.json()["review_source"] == ("saved" if verdict else "attempt")
    assert not repeated.json()["model_called"]
    assert len(calls) == (1 if verdict else 2)
    bars = list(market.tape)
    signal_index = len(bars) - BARS + index
    bars[signal_index + 1] = replace(bars[signal_index + 1], high=bars[signal_index + 1].high + 50)
    feed[0] = assemble(bars, market.fetched_ms)
    assert client.get(f"/pa-agent/rule?close_ms={close}").json()["fingerprint"] == args["fingerprint"]
    bars[signal_index - 80] = replace(bars[signal_index - 80], high=bars[signal_index - 80].high + 50)
    feed[0] = assemble(bars, market.fetched_ms)
    assert client.post("/pa-agent/review", json=args).status_code == 409
    assert not client.get("/pa-agent/market").json()["trades"]


def test_review_history_is_read_only_and_independent_of_replay(monkeypatch, tmp_path):
    from apps.api.main import app
    from paknow.pa_review import ReviewLedger

    ledger = ReviewLedger(tmp_path / "history.db")
    monkeypatch.setattr(pa_agent, "review_ledger", ledger)

    def unexpected(*_args, **_kwargs):
        pytest.fail("history must not consult live replay, the corpus, or a model")

    for name in ("_market", "corpus", "explain", "fingerprint"):
        monkeypatch.setattr(pa_agent, name, unexpected)
    client = TestClient(app)
    base = "/pa-agent/review-history"
    assert client.get(base).json() == {"total": 0, "records": []}
    assert client.get(f"{base}/{'a' * 64}").status_code == 404
    assert not ledger.path.exists()

    def result(close_ms, verdict, name):
        return {"rule": {"close_ms": close_ms, "state": "short"}, "retrieval_status": "ok",
                "commentary_status": "ok", "commentary": {"verdict": verdict, "review": {"text": name}},
                "sources": [{"id": "excerpt", "text": name, "reading_context": "saved parent"}]}

    first, second, older, failed = (letter * 64 for letter in "abcd")
    current = result(START, "approve", "same signal, first review")
    other_profile = result(START, "reject", "same signal, different review")
    old_window = result(START - STEP * BARS, "wait", "old window review")
    for key, row in ((first, current), (second, other_profile), (older, old_window)):
        assert ledger.run(key, lambda: row) == row
    blocked = {"rule": {"close_ms": START + STEP}, "model_called": True, "commentary_status": "unavailable"}
    assert ledger.run(failed, lambda: blocked, review_input=(blocked["rule"], ())).get("attempt_id")
    snapshot = ledger.path.read_bytes()
    monkeypatch.setattr(ledger, "_connect", unexpected)

    page = client.get(base, params={"limit": 2})
    assert page.headers["Cache-Control"] == "no-store"
    assert page.json() == {"total": 3, "records": [
        {"fingerprint": second, "number": 2, "close_ms": START, "direction": "short", "verdict": "reject"},
        {"fingerprint": first, "number": 1, "close_ms": START, "direction": "short", "verdict": "approve"},
    ]}
    assert client.get(base, params={"offset": 2}).json()["records"][0]["fingerprint"] == older
    for key, expected in ((first, current), (second, other_profile), (older, old_window)):
        detail = client.get(f"{base}/{key}")
        assert detail.json() == expected and detail.headers["Cache-Control"] == "no-store"
    assert client.get(f"{base}/{failed}").status_code == 404
    assert client.get(f"{base}/not-a-fingerprint").status_code == 422
    assert client.get(base, params={"limit": 101}).status_code == 422
    assert client.get(base, params={"offset": -1}).status_code == 422
    assert ledger.path.read_bytes() == snapshot


def test_replay_uses_another_profile_and_preserves_exact_saved_records(market, monkeypatch, tmp_path):
    from copy import deepcopy

    from apps.api.main import app
    from paknow import pa_review

    ledger = pa_review.ReviewLedger(tmp_path / "history.db")
    monkeypatch.setattr(pa_agent, "review_ledger", ledger)
    monkeypatch.setattr(pa_agent.market_feed, "get", lambda **kwargs: (market, False))
    i = next(i for i, key in enumerate(market.review_keys) if key)
    close_ms = market.bars[i].ts_open + STEP
    rule, bars = market.at(close_ms)
    saved_rule = deepcopy(rule)
    saved_rule["facts"]["higher_ema20"] += 5  # original, different hourly warmup
    old = {"rule": saved_rule, "retrieval_status": "ok", "commentary_status": "ok",
           "commentary": {"verdict": "approve", "paper_plan": _paper_plan(rule)}}
    previous_profile = "deepseek" if pa_review.settings.pa_cloud_provider == "terra" else "terra"
    with monkeypatch.context() as patch:
        patch.setattr(pa_review.settings, "pa_cloud_provider", previous_profile)
        old_key = pa_review.fingerprint(saved_rule, bars)
    assert ledger.run(old_key, lambda: old) == old
    active_key = market.review_keys[i]
    assert ledger.matching({active_key: (rule, bars)}) == {}
    assert ledger.replay_matches({active_key: (rule, bars)}) == {active_key: old}
    changed = (replace(bars[0], high=bars[0].high + 1), *bars[1:])
    assert ledger.replay_matches({pa_review.fingerprint(rule, changed): (rule, changed)}) == {}
    client = TestClient(app)
    saved = client.get("/pa-agent/rule", params={"close_ms": close_ms}).json()
    assert saved["saved_review"] == old and saved["review_status"] == "approve"
    view = client.get("/pa-agent/market").json()
    assert view["reviews"][i] == "approve" and len(view["orders"]) == len(view["trades"]) == 1
    new = {**old, "rule": rule, "commentary": {"verdict": "wait"}}
    ledger.run(active_key, lambda: new)
    current = client.get("/pa-agent/market").json()
    assert current["reviews"][i] == "wait" and not current["orders"]
    assert client.get(f"/pa-agent/review-history/{old_key}").json() == old


def test_review_history_replay_can_jump_one_candle_outside_live_window(monkeypatch, tmp_path):
    from apps.api.main import app
    from paknow.pa_market import PublicMarket
    from paknow.pa_review import ReviewLedger

    bars = tape(BARS + WARMUP + 8)
    archive = assemble(bars[:-4], bars[-5].ts_open + STEP)
    live = assemble(bars, bars[-1].ts_open + STEP)
    close_ms = live.bars[0].ts_open
    assert archive.locate(close_ms) < 4
    feed = PublicMarket(tmp_path)
    feed._save(archive)
    feed.path.rename(tmp_path / "btc-usdt-swap-15m-archive-okx.json")
    feed._save(live)
    monkeypatch.setattr(pa_agent, "market_feed", feed)
    ledger = ReviewLedger(tmp_path / "replay.db")
    monkeypatch.setattr(pa_agent, "review_ledger", ledger)
    key = "e" * 64
    state, _ = archive.at(close_ms)
    record = {"rule": state, "retrieval_status": "ok", "commentary_status": "ok",
              "commentary": {"verdict": "wait"}}
    ledger.run(key, lambda: record)
    monkeypatch.setattr(feed, "get", lambda **kwargs: pytest.fail("history must not fetch live market"))
    client = TestClient(app)
    path = f"/pa-agent/review-history/{key}"
    view = client.get(path + "/replay").json()
    assert view["history_close_ms"] == close_ms and not view["cloud_available"]
    assert view["bars"][0]["ts_open"] + STEP == close_ms
    selected = client.get("/pa-agent/rule", params={"close_ms": close_ms, "history_close_ms": close_ms})
    assert selected.status_code == 200 and selected.json()["rule"]["close_ms"] == close_ms
    (tmp_path / "btc-usdt-swap-15m-archive-okx.json").unlink()
    feed.path.unlink()
    assert client.get(path + "/replay").status_code == 404
    assert client.get(path).json() == record


def test_failed_model_refresh_and_explicit_retry_reuse_evidence(market, monkeypatch, tmp_path):
    from apps.api.main import app
    from paknow.gateway import ModelOutputError
    from paknow.pa_review import ReviewLedger
    from paknow.retrieval.pa_packet import Packet

    idx = next(i for i, key in enumerate(market.review_keys) if key)
    close_ms = market.bars[idx].ts_open + STEP
    body = {"close_ms": close_ms, "fingerprint": market.review_keys[idx]}
    ledger = ReviewLedger(tmp_path / "reviews.db")
    monkeypatch.setattr(pa_agent, "review_ledger", ledger)
    monkeypatch.setattr(pa_agent.market_feed, "get", lambda **_kwargs: (market, False))
    monkeypatch.setattr(pa_analysis, "settings", SimpleNamespace(has_cloud=True, pa_review_max_tokens=24_576))
    monkeypatch.setattr(pa_analysis, "corpus", lambda: ("sealed-test", frozenset({"doc"})))
    lookups, calls = [], []
    sources = [{"id": role, "text": f"Exact original passage for {role}.", "role": role,
                "verified": True, "passage_id": role} for role in ("support", "risk", "confirmation")]
    def retrieve(setup):
        lookups.append(setup)
        return Packet({s["role"]: "Question" for s in sources}, sources, [], "ok", [], "sealed-test")

    def model(schema, messages, **_kwargs):
        text = json.loads(messages[1]["content"])
        assert text["sources"] == sources and len(text["previous_80_oldest_first"]) == LOOKBACK
        calls.append(True)
        if len(calls) <= 2:
            failure = ModelOutputError("do not expose raw model reply" if len(calls) == 2 else "", "unparseable")
            if len(calls) == 1:
                failure.finish_reason = "length"
            raise failure
        thesis, risk, confirmation = [Point(text="The inputs support this conditional interpretation.", evidence_ids=[s["id"]],
                                              quote={"evidence_id": s["id"], "text": s["text"]}) for s in sources]
        return schema(verdict="wait", support_status="unresolved", thesis=thesis, context=thesis, risks=risk, watch=confirmation,
                      review=risk, paper_plan=None)

    monkeypatch.setattr(pa_analysis, "retrieve_packet", retrieve)
    monkeypatch.setattr(pa_analysis.gateway, "chat_json", model)
    client = TestClient(app)
    review_path, rule_path = "/pa-agent/review", f"/pa-agent/rule?close_ms={close_ms}"
    assert client.post(review_path, json={**body, "retry_attempt_id": "1" * 32}).status_code == 409
    first = client.post(review_path, json=body).json()
    assert first["review_status"] == "pending" and first["model_called"] and first["attempt_id"]
    assert first["commentary_failure"] == "length" and first["attempt_completed_ms"] > 0
    assert "do not expose" not in json.dumps(first)
    assert not first["orders"] and not first["trades"]
    monkeypatch.setattr(pa_agent, "review_ledger", ReviewLedger(ledger.path))
    reloaded = client.get(rule_path).json()
    assert not reloaded["saved_review"] and reloaded["last_attempt"]["attempt_id"] == first["attempt_id"]
    assert client.get("/pa-agent/market").json()["reviews"][idx] == "pending"
    repeated = client.post(review_path, json=body)
    assert repeated.json()["review_source"] == "last_attempt" and not repeated.json()["model_called"]
    assert "model;" not in repeated.headers["server-timing"] and "lookup;" not in repeated.headers["server-timing"]
    assert len(lookups) == len(calls) == 1

    new = client.post(review_path, json={**body, "retry_attempt_id": first["attempt_id"]})
    assert new.json()["model_called"] and new.json()["evidence_source"] == "saved"
    assert new.json()["attempt_id"] != first["attempt_id"] and "lookup;" not in new.headers["server-timing"]
    stale = client.post(review_path, json={**body, "retry_attempt_id": first["attempt_id"]})
    assert stale.status_code == 409 and stale.headers["X-PA-Review-State"] == "attempt_changed"
    assert client.get(rule_path).json()["last_attempt"]["attempt_id"] == new.json()["attempt_id"]
    assert len(lookups) == 1 and len(calls) == 2
    valid = client.post(review_path, json={**body, "retry_attempt_id": new.json()["attempt_id"]}).json()
    assert valid["model_called"] and valid["evidence_source"] == "saved" and valid["review_status"] == "wait"
    assert not valid["orders"] and not valid["trades"]
    assert client.get(rule_path).json()["last_attempt"] is None
    assert client.post(review_path, json=body).json()["review_source"] == "saved"
    assert len(lookups) == 1 and len(calls) == 3


def test_rule_reports_existing_review_without_starting_another(market, monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from apps.api.main import app
    from paknow.pa_review import ReviewLedger

    ledger = ReviewLedger(tmp_path / "progress.db")
    monkeypatch.setattr(pa_agent, "review_ledger", ledger)
    monkeypatch.setattr(pa_agent.market_feed, "get", lambda **_: (market, False))
    i = next(i for i, key in enumerate(market.review_keys) if key)
    close_ms, key = market.bars[i].ts_open + STEP, market.review_keys[i]
    entered, release = Event(), Event()
    calls = []

    def model(state, bars, **kwargs):
        calls.append(state["close_ms"])
        entered.set()
        assert release.wait(5)
        return {"rule": state, "retrieval_status": "ok", "commentary_status": "ok", "commentary": {"verdict": "wait"}}

    monkeypatch.setattr(pa_agent, "explain", model)
    client = TestClient(app)
    args = {"close_ms": close_ms, "fingerprint": key}
    path = f"/pa-agent/rule?close_ms={close_ms}"
    with ThreadPoolExecutor(max_workers=1) as pool:
        active = pool.submit(client.post, "/pa-agent/review", json=args)
        try:
            assert entered.wait(5)
            state = client.get(path).json()
            assert state["review_in_progress"] and state["review_status"] == "pending"
            duplicate = client.post("/pa-agent/review", json=args)
            assert duplicate.status_code == 429 and duplicate.headers["X-PA-Review-State"] == "in_progress"
            assert calls == [close_ms]
        finally:
            release.set()
        assert active.result(timeout=5).json()["review_status"] == "wait"
    state = client.get(path).json()
    assert not state["review_in_progress"] and state["review_status"] == "wait"


def test_batch_is_explicit_pinned_parallel_and_deduplicated(market, monkeypatch, tmp_path):
    from threading import Event, Lock
    from time import monotonic, sleep

    from apps.api.main import app
    from paknow.pa_batch import BatchQueue
    from paknow.pa_review import LOCAL_REVIEW_PARALLELISM, ReviewLedger

    ledger = ReviewLedger(tmp_path / "batch.db", parallelism=LOCAL_REVIEW_PARALLELISM)
    monkeypatch.setattr(pa_agent, "review_ledger", ledger)
    monkeypatch.setattr(pa_agent, "batch_queue", BatchQueue())
    monkeypatch.setattr(pa_agent, "settings", SimpleNamespace(has_cloud=True))
    monkeypatch.setattr(pa_agent.market_feed, "get", lambda **_kwargs: (market, False))
    active = 0
    calls = []
    lock, filled, release = Lock(), Event(), Event()
    candidates = [(bar.ts_open + STEP, key) for bar, key in zip(market.bars, market.review_keys) if key]
    (skip_close, skip_key), (first_close, first_key), *_ = candidates
    expected_overlap = min(LOCAL_REVIEW_PARALLELISM, len(candidates) - 1)
    old_rule, _ = market.at(skip_close)
    ledger.run(skip_key, lambda: {"rule": old_rule, "retrieval_status": "ok", "commentary_status": "ok",
                                  "commentary": {"verdict": "reject"}})

    def fake_review(rule, bars, *, cloud, timings, model_admitted, stop_dispatch, inputs_current):
        nonlocal active
        assert cloud and model_admitted() and inputs_current() and len(bars) == LOOKBACK + 1
        assert bars[-1].ts_open + STEP == rule["close_ms"]
        with lock:
            calls.append(rule["close_ms"])
            active += 1
            assert active <= LOCAL_REVIEW_PARALLELISM
            if active == expected_overlap:
                filled.set()
        try:
            assert release.wait(6)
            timings["model"] = 0.01
            return {"rule": rule, "retrieval_status": "ok", "commentary_status": "ok", "commentary": {"verdict": "wait"}}
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(pa_agent, "explain", fake_review)
    client = TestClient(app)
    path = "/pa-agent/review-batch"
    body = {"id": "1" * 32, "snapshot": pa_agent._snapshot(market), "confirm": True}
    assert client.get(path).json()["batch"] is None and not calls
    assert client.post(path, json={**body, "snapshot": [0, 0, 0]}).status_code == 409
    assert not calls
    running = client.post(path, json=body)
    try:
        assert running.status_code == 202 and running.json()["total"] == len(candidates) - 1
        assert filled.wait(5)
        assert client.post(path, json={**body, "id": "2" * 32}).json()["id"] == body["id"]
        assert client.post("/pa-agent/review", json={"close_ms": first_close, "fingerprint": first_key}).status_code == 429
    finally:
        release.set()
    until = monotonic() + 10
    while monotonic() < until:
        batch = client.get(path).json()["batch"]
        if batch["status"] == "done":
            break
        sleep(0.01)
    assert batch["counts"] == {"wait": len(candidates) - 1}
    assert batch["model_attempts"] == len(candidates) - 1
    assert len(calls) == len(set(calls)) == len(candidates) - 1 and skip_close not in calls
    assert client.post(path, json=body).json()["status"] == "done"


def test_provider_fatal_stops_waiters_at_cloud_gate_after_local_retrieval(market, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock

    from paknow.pa_review import CLOUD_REVIEW_PARALLELISM, ModelSlots
    from paknow.retrieval.pa_packet import Packet

    i = next(i for i, key in enumerate(market.review_keys) if key)
    rule, bars = market.at(market.bars[i].ts_open + STEP)
    slots = CLOUD_REVIEW_PARALLELISM
    width = slots + 2
    lock, local_done, cloud_full, stop = Lock(), Event(), Event(), Event()
    lookups = []
    calls = []

    def retrieve(setup):
        with lock:
            lookups.append(setup)
            if len(lookups) == width:
                local_done.set()
        return Packet({"support": "test"}, [{"id": "a", "text": "original text"}], [], "ok", [], "test")

    def provider(response, *_args, **_kwargs):
        with lock:
            calls.append(True)
            is_first = len(calls) == 1
            if len(calls) == slots:
                cloud_full.set()
        if is_first:
            assert local_done.wait(4) and cloud_full.wait(4)
            response["commentary_status"] = "provider_rate_limit"
        else:
            assert stop.wait(4)
            response["commentary_status"] = "ok"

    monkeypatch.setattr(pa_analysis, "settings", SimpleNamespace(has_cloud=True))
    monkeypatch.setattr(pa_analysis, "retrieve_packet", retrieve)
    monkeypatch.setattr(pa_analysis, "retrieval_slots", ModelSlots(width))
    monkeypatch.setattr(pa_analysis, "model_slots", ModelSlots(slots))
    monkeypatch.setattr(pa_analysis, "_add_model", provider)
    with ThreadPoolExecutor(width) as pool:
        jobs = [pool.submit(pa_analysis.explain, rule, bars, cloud=True,
                            model_admitted=lambda: not stop.is_set(), stop_dispatch=stop.set) for _ in range(width)]
        statuses = [job.result(6)["commentary_status"] for job in jobs]
    assert len(calls) == slots and len(lookups) == width
    assert statuses.count("provider_rate_limit") == 1
    assert statuses.count("ok") == slots - 1
    assert statuses.count("batch_stopped") == width - slots


def test_window_roll_reuses_original_decision_only_with_identical_causal_input(market, monkeypatch, tmp_path):
    from copy import deepcopy

    from paknow.pa_review import ReviewLedger, _same_rule_after_warmup, fingerprint, settings

    ledger = ReviewLedger(tmp_path / "replay.db")
    i = next(i for i, key in enumerate(market.review_keys) if key)
    rule, bars = market.at(market.bars[i].ts_open + STEP)
    saved = {"rule": rule, "retrieval_status": "ok", "commentary_status": "ok",
             "commentary": {"verdict": "wait"}}
    ledger.run(fingerprint(rule, bars), lambda: saved)
    drifted = deepcopy(rule)
    drifted["facts"]["higher_ema20"] += 0.005
    drifted["facts"]["higher_ema_delta_atr"] += 0.0001
    key = fingerprint(drifted, bars)
    assert ledger.matching({key: (drifted, bars)}) == {key: saved}
    assert ledger.get((key,)) == {}  # reading does not rewrite or duplicate a review

    # Reproduce the scale of the BTC mismatch: ~$1.5 EMA drift on a $65k
    # candle, with the same observed hourly close and slope band.
    prior = {"facts": {"higher_close": 64434.1, "atr14": 377.8, "higher_ema20": 64668.08,
                       "higher_ema_delta_atr": -0.1844}}
    current = {"facts": {**prior["facts"], "higher_ema20": 64669.60, "higher_ema_delta_atr": -0.1860}}
    assert _same_rule_after_warmup(prior, current)
    a = {"facts": {**prior["facts"], "higher_ema_delta_atr": -0.0995}}
    b = {"facts": {**current["facts"], "higher_ema_delta_atr": -0.1005}}
    assert not _same_rule_after_warmup(a, b)
    assert not _same_rule_after_warmup({"facts": {**prior["facts"], "higher_close": 64669.0}},
                                       {"facts": {**current["facts"], "higher_close": 64669.0}})

    racing = ReviewLedger(tmp_path / "race.db")
    match = racing.matching

    def match_while_first_review_commits(inputs):
        result = match(inputs)
        if not result:
            racing.run(fingerprint(rule, bars), lambda: saved)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(racing, "matching", match_while_first_review_commits)
        assert racing.run(key, lambda: pytest.fail("no second model call"), review_input=(drifted, bars)) == saved
        assert racing.get((key,)) == {}

    revised = (replace(bars[0], high=bars[0].high + 1), *bars[1:])
    assert ledger.matching({fingerprint(drifted, revised): (drifted, revised)}) == {}
    changed = {**drifted, "original_state": "wait"}
    assert ledger.matching({fingerprint(changed, bars): (changed, bars)}) == {}
    far = deepcopy(drifted)
    far["facts"]["higher_ema20"] += 100
    assert ledger.matching({fingerprint(far, bars): (far, bars)}) == {}
    conflict = deepcopy(rule)
    conflict["facts"]["higher_ema20"] -= 0.002
    conflicting = {**saved, "rule": conflict, "commentary": {"verdict": "reject"}}
    conflict_key = fingerprint(conflict, bars)
    ledger.run(conflict_key, lambda: conflicting)
    assert ledger.matching({key: (drifted, bars)}) == {}
    assert ledger.matching({conflict_key: (conflict, bars)}) == {conflict_key: conflicting}
    for name, value in ((settings.pa_cloud_provider + "_model", "other-test-model"),
                        (settings.pa_cloud_provider + "_effort", "other-test-effort"),
                        ("pa_cloud_provider", "terra" if settings.pa_cloud_provider == "deepseek" else "deepseek")):
        with monkeypatch.context() as patch:
            patch.setattr(settings, name, value)
            assert ledger.matching({fingerprint(rule, bars): (rule, bars)}) == {}
    assert ledger.matching({fingerprint(rule, bars): (rule, bars)})


@pytest.mark.parametrize("prior_policy", ["paknow-paper-plan-v3", "paknow-paper-plan-v4-bm25",
                                          "paknow-paper-plan-v9-context-roles"])
def test_previous_policy_stays_in_ledger_but_does_not_replay(market, monkeypatch, tmp_path, prior_policy):
    from paknow import pa_review

    i = next(i for i, key in enumerate(market.review_keys) if key)
    rule, bars = market.at(market.bars[i].ts_open + STEP)
    ledger = pa_review.ReviewLedger(tmp_path / "reviews.db")
    current = pa_review.fingerprint(rule, bars)
    with monkeypatch.context() as env:
        env.setattr(pa_review, "POLICY_VERSION", prior_policy)
        legacy = pa_review.fingerprint(rule, bars)
    assert legacy != current
    saved = {"rule": rule, "retrieval_status": "ok", "commentary_status": "ok",
             "commentary": {"verdict": "approve", "paper_plan": _paper_plan(rule)}}
    ledger.run(legacy, lambda: saved)
    assert ledger.matching({current: (rule, bars)}) == {}
    assert ledger.get((legacy, current)) == {legacy: saved}
    revised = (replace(bars[0], high=bars[0].high + 1), *bars[1:])
    assert ledger.matching({pa_review.fingerprint(rule, revised): (rule, revised)}) == {}


def test_saved_decision_is_readable_while_new_review_has_the_slot(tmp_path):
    from paknow.pa_review import ReviewBusy, ReviewLedger

    ledger = ReviewLedger(tmp_path / "reviews.db")
    decision = {"retrieval_status": "ok", "commentary_status": "ok", "commentary": {"verdict": "wait"}}
    ledger.run("saved", lambda: decision)

    def unexpected_analysis():
        pytest.fail("this analysis must not run")

    def analyze_while_busy():
        assert ledger.run("saved", unexpected_analysis) == decision
        with pytest.raises(ReviewBusy):
            ledger.run("another", unexpected_analysis)
        return decision

    assert ledger.run("new", analyze_while_busy) == decision
