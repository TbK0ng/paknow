"""Same-origin routes for the PA Agent workspace."""
from pathlib import Path
from threading import Event, Lock
from time import monotonic_ns, perf_counter
from typing import Literal

from fastapi import APIRouter, HTTPException, Path as PathParam, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from paknow.config import settings
from paknow.evidence.citations import citation_ready_hits, render_citation
from paknow.pa_analysis import explain, review_evidence_ready
from paknow.pa_batch import batch_queue
from paknow.pa_market import Market, market_feed
from paknow.pa_preview import EvidencePreviews
from paknow.pa_review import (CLOUD_REVIEW_PARALLELISM, LOCAL_REVIEW_PARALLELISM, POLICY_VERSION,
                              ReviewAttemptChanged, ReviewBusy, ReviewInput, fingerprint, review_ledger, verdict)
from paknow.pa_rules import STEP, VERSION
from paknow.retrieval.pa_corpus import corpus
from paknow.retrieval.runtime import Priority, priority, runtime_stats

router = APIRouter(prefix="/pa-agent", tags=["PA Agent"])
_PAGE = Path(__file__).resolve().parent.parent / "viewer" / "pa_agent.html"
_evidence_previews = EvidencePreviews()
_projection_lock = Lock()
_projection_sequence = 0


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    close_ms: int = Field(gt=0, strict=True)
    cloud: bool = False


class CandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    close_ms: int = Field(gt=0, strict=True)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewRequest(CandidateRequest):
    retry_attempt_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    evidence_token: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


class BatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    snapshot: tuple[int, int, int]
    confirm: Literal[True]


def _snapshot(data: Market) -> tuple[int, int, int]:
    return data.bars[0].ts_open, data.last_ms, data.fetched_ms


@router.get("", include_in_schema=False)
def page() -> FileResponse:
    return FileResponse(_PAGE)


def _market(*, refresh: bool = False):
    try:
        return market_feed.get(refresh=refresh)
    except LookupError as exc:
        raise HTTPException(503, detail=str(exc)) from exc


def _selected(close_ms: int, history_close_ms: int | None = None):
    try:
        market, stale = (market_feed.history(history_close_ms), True) if history_close_ms else _market()
        index = market.locate(close_ms)
        rule, bars = market.at(close_ms)
    except LookupError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    return market, index, rule, bars, stale


def _reviews(data: Market):
    return review_ledger.replay_matches({key: data.at(bar.ts_open + STEP)
                                         for key, bar in zip(data.review_keys, data.bars) if key})


def _projection_inputs(data: Market) -> tuple[dict, int]:
    # A newer sequence must come from a later ledger read, not merely a
    # response which finished projecting and serializing later.
    global _projection_sequence
    with _projection_lock:
        reviews = _reviews(data)
        _projection_sequence = max(_projection_sequence + 1, monotonic_ns() // 1000)
        return reviews, _projection_sequence


def _market_payload(data: Market, *, stale: bool, history_close_ms: int | None = None):
    reviews, sequence = _projection_inputs(data)
    return {**data.payload(stale=stale, reviews=reviews), "cloud_available": not history_close_ms and settings.has_cloud,
            "history_close_ms": history_close_ms, "projection_seq": sequence,
            "rule_version": VERSION, "review_version": POLICY_VERSION}


@router.get("/market")
def market(response: Response, refresh: bool = False):
    response.headers["Cache-Control"] = "no-store"
    data, stale = _market(refresh=refresh)
    return _market_payload(data, stale=stale)


@router.get("/rule")
def rule(response: Response, close_ms: int = Query(gt=0), history_close_ms: int | None = Query(None, gt=0)):
    response.headers["Cache-Control"] = "no-store"
    market, index, result, bars, stale = _selected(close_ms, history_close_ms)
    key = market.review_keys[index]
    active = bool(key and not history_close_ms and review_ledger.in_progress(close_ms))
    reviews, sequence = _projection_inputs(market) if key else ({}, 0)
    saved = reviews.get(key) if key else None
    previous = review_ledger.last_attempt(key, (result, bars)) if key and not saved and not history_close_ms else None
    projection = {**market.review_projection(reviews), "projection_seq": sequence} if saved else None
    return {"rule": result, "stale": stale, "fingerprint": key, "review_status": verdict(saved) if key else None,
            "review_source": "saved" if saved else "last_attempt" if previous else None,
            "saved_review": saved, "last_attempt": previous, "review_in_progress": active and not saved,
            "snapshot": [market.bars[0].ts_open, market.last_ms, market.fetched_ms],
            "projection": projection}


@router.get("/review-history")
def review_history(response: Response, offset: int = Query(0, ge=0, le=2**63 - 1), limit: int = Query(50, ge=1, le=100)):
    response.headers["Cache-Control"] = "no-store"
    return review_ledger.history_page(offset=offset, limit=limit)


@router.get("/review-history/{key}")
def review_history_record(response: Response, key: str = PathParam(pattern=r"^[0-9a-f]{64}$")):
    record = review_ledger.history_record(key)
    if record is None:
        raise HTTPException(404, detail="没有这笔历史复核记录。")
    response.headers["Cache-Control"] = "no-store"
    return record


@router.get("/review-history/{key}/replay")
def review_history_replay(response: Response, key: str = PathParam(pattern=r"^[0-9a-f]{64}$")):
    record = review_history_record(response, key)
    close_ms = record["rule"]["close_ms"]
    data, _, _, _, _ = _selected(close_ms, close_ms)
    return _market_payload(data, stale=True, history_close_ms=close_ms)


@router.get("/evidence/{evidence_id}")
def evidence(evidence_id: str, context: bool = False):
    _, docs = corpus()
    hits = citation_ready_hits([{"evidence_id": evidence_id}], doc_ids=docs)
    if not hits:
        raise HTTPException(404, detail="没有可引用的原文。")
    hit = hits[0]
    response = {"text": hit["text"], "label": render_citation(hit, hit["doc_title"]),
                "provenance": hit["provenance"]}
    if context and hit["parent_id"]:
        parents = citation_ready_hits([{"evidence_id": hit["parent_id"]}], doc_ids=docs, allow_parent=True)
        if parents and parents[0]["doc_id"] == hit["doc_id"]:
            response["context"] = parents[0]["text"]
    return response


@router.post("/analyze")
def analyze(req: AnalyzeRequest):
    data, index, result, bars, stale = _selected(req.close_ms)
    if data.review_keys[index]:
        # A candidate has exactly one authoritative analysis path, /review.
        raise HTTPException(409, detail="PA 候选请使用第二层入场复核。")
    return {**explain(result, bars, cloud=req.cloud), "stale": stale, "snapshot": _snapshot(data)}


def _candidate_key(data: Market, index: int, state: dict, bars, expected: str) -> str:
    key = data.review_keys[index]
    if key is None or key != expected or fingerprint(state, bars) != key:
        raise HTTPException(409, detail="候选、背景、语料或云模型配置已更新，请刷新后重新选择。")
    return key


@router.post("/review-evidence")
def review_evidence(req: CandidateRequest, response: Response):
    data, index, state, bars, stale = _selected(req.close_ms)
    key = _candidate_key(data, index, state, bars, req.fingerprint)
    packet = explain(state, bars, cloud=False)
    _candidate_key(data, index, state, bars, req.fingerprint)
    generation = packet.get("corpus_generation")
    if generation is not None and generation != corpus()[0]:
        raise HTTPException(409, detail="取证期间语料已更新，请刷新行情后重新取证。")
    token = (_evidence_previews.put(key, packet) if generation not in (None, "unsealed")
             and review_evidence_ready(packet) else None)
    response.headers["Cache-Control"] = "no-store"
    return {**packet, "stale": stale, "snapshot": _snapshot(data), "fingerprint": key,
            "evidence_token": token, "model_called": False}


def _run_review(key: str, inputs: ReviewInput, retry_id: str | None, timings: dict[str, float],
                stop: Event | None = None, *, preview: dict | None = None) -> tuple[dict, bool]:
    state, bars = inputs
    attempted = False

    def analyze_candidate():
        nonlocal attempted
        attempted = True
        def current():
            return fingerprint(state, bars) == key

        if not current():
            return {"rule": state, "commentary_status": "inputs_changed"}
        if stop is not None and stop.is_set():
            return {"rule": state, "commentary_status": "batch_stopped"}
        kwargs = {"inputs_current": current}
        if preview is not None:
            kwargs["cached_evidence"] = preview
        elif retry_id:
            if previous := review_ledger.last_attempt(key, inputs):
                kwargs["cached_evidence"] = previous
        if stop is not None:
            kwargs["model_admitted"] = lambda: not stop.is_set() and current()
            kwargs["stop_dispatch"] = stop.set
        token = priority.set(Priority(manual=stop is None))
        try:
            reply = explain(state, bars, cloud=True, timings=timings, **kwargs)
            if not current():
                return {"rule": state, "commentary_status": "inputs_changed"}
            return {**reply, "model_called": "model" in timings}
        finally:
            priority.reset(token)

    # Existing manual and batch paths share the singleton ledger and admission.
    admission = {"queue": True} if stop is not None else {}
    # A saved decision used by the replay must not be billed again.
    if saved := review_ledger.replay_matches({key: inputs}).get(key):
        return saved, False
    result = review_ledger.run(key, analyze_candidate, review_input=inputs, retry_attempt_id=retry_id, **admission)
    return result, attempted


def _batch_item(key: str, inputs: ReviewInput, retry_id: str | None, stop: Event) -> dict:
    if stop.is_set():
        return {"status": "not_started"}
    timings: dict[str, float] = {}
    try:
        result, attempted = _run_review(key, inputs, retry_id, timings, stop)
    except ReviewAttemptChanged:
        return {"status": "pending", "reason": "attempt_changed"}
    status = verdict(result)
    data = {"model_called": "model" in timings}
    if status != "pending":
        return {"status": status, **data}
    if not attempted:
        return {"status": "pending", "reason": "attempt_changed", **data}
    detail = result.get("commentary_status")
    if detail == "inputs_changed":
        return {"status": "not_started", "reason": "inputs_changed", **data}
    if detail == "batch_stopped":
        if not stop.is_set():
            return {"status": "not_started", "reason": "inputs_changed", **data}
        return {"status": "not_started", **data}
    reason = result.get("commentary_failure") if detail == "unavailable" else detail
    return {"status": "blocked" if detail == "insufficient_evidence" else "failed", "reason": reason, **data}


@router.get("/retrieval-stats")
def retrieval_stats(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return runtime_stats()


@router.get("/review-batch")
def review_batch_status(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {"batch": batch_queue.status(), "parallelism": LOCAL_REVIEW_PARALLELISM,
            "cloud_parallelism": CLOUD_REVIEW_PARALLELISM}


@router.post("/review-batch", status_code=202)
def start_review_batch(req: BatchRequest):
    current = batch_queue.status()
    if current and (current["id"] == req.id or current["status"] in {"running", "stopping"}):
        return current
    if not settings.has_cloud:
        raise HTTPException(409, detail="未配置复核模型。")
    data, _ = _market()
    if req.snapshot != _snapshot(data):
        raise HTTPException(409, detail="行情窗口已变，请更新页面后再提交批量复核。")
    saved = _reviews(data)
    jobs = []
    for bar, key in zip(data.bars, data.review_keys, strict=True):
        if not key or key in saved:
            continue
        close_ms = bar.ts_open + STEP
        inputs = data.at(close_ms)
        last = review_ledger.last_attempt(key, inputs)
        receipt = last["attempt_id"] if last else None
        jobs.append((close_ms, lambda stop, k=key, i=inputs, r=receipt: _batch_item(k, i, r, stop)))
    return batch_queue.submit(req.id, req.snapshot, jobs)


@router.post("/review-batch/{batch_id}/stop")
def stop_review_batch(batch_id: str):
    if (batch := batch_queue.stop(batch_id)) is not None:
        return batch
    raise HTTPException(404, detail="批量任务不存在。")


@router.post("/review")
def review(req: ReviewRequest, response: Response):
    start = perf_counter()
    data, index, result, bars, stale = _selected(req.close_ms)
    timings = {"snapshot": perf_counter() - start}
    key = _candidate_key(data, index, result, bars, req.fingerprint)
    preview = None
    if req.evidence_token is not None:
        preview = _evidence_previews.matching(req.evidence_token, key, corpus()[0])
        if preview is None:
            raise HTTPException(409, detail="本次证据预览已失效，请重新取证后再复核。",
                                headers={"X-PA-Review-State": "preview_expired"})
    start = perf_counter()
    try:
        reviewed, attempted = _run_review(key, (result, bars), req.retry_attempt_id, timings, preview=preview)
    except ReviewAttemptChanged as exc:
        raise HTTPException(409, detail="复核记录已变更，未再次调用模型。",
                            headers={"X-PA-Review-State": "attempt_changed"}) from exc
    except ReviewBusy as exc:
        if review_ledger.in_progress(req.close_ms):
            raise HTTPException(429, detail="这笔候选已有复核在进行，不会重复调用模型。",
                                headers={"X-PA-Review-State": "in_progress"}) from exc
        raise HTTPException(429, detail="复核名额已满，本次未调用模型，请稍后再试。") from exc
    _candidate_key(data, index, result, bars, req.fingerprint)
    if reviewed.get("commentary_status") == "inputs_changed":
        raise HTTPException(409, detail="复核期间输入已更新，请刷新行情后重新取证。")
    timings["review"] = perf_counter() - start
    # Re-project only for this snapshot. The viewer must verify its window.
    start = perf_counter()
    reviews, sequence = _projection_inputs(data)
    projection = data.review_projection(reviews)
    timings["projection"] = perf_counter() - start
    response.headers["Server-Timing"] = ", ".join(f"{stage};dur={elapsed * 1000:.1f}"
                                                  for stage, elapsed in timings.items())
    response.headers["Cache-Control"] = "no-store"
    return {**reviewed, "stale": stale, "review_status": verdict(reviews.get(key)),
            "review_source": "attempt" if attempted else "last_attempt" if "attempt_id" in reviewed else "saved",
            "model_called": "model" in timings,
            "snapshot": _snapshot(data), "projection_seq": sequence, **projection}
