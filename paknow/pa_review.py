"""Causal, versioned second-layer approvals for offline PA replay."""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import asdict
from hashlib import sha256
import json
from math import isclose
from pathlib import Path
import sqlite3
from threading import Condition
from time import time_ns
from typing import Any, Callable
from uuid import uuid4

from paknow.config import settings
from paknow.pa_rules import VERSION as RULE_VERSION, Bar
from paknow.retrieval.pa_corpus import corpus

POLICY_VERSION = "paknow-paper-plan-v9-context-roles"
LOCAL_REVIEW_PARALLELISM = settings.pa_local_parallelism
CLOUD_REVIEW_PARALLELISM = 8


def fingerprint(state: dict[str, Any], bars: tuple[Bar, ...]) -> str:
    return _fingerprint(state, bars, POLICY_VERSION)


def _fingerprint(state: dict[str, Any], bars: tuple[Bar, ...], policy: str) -> str:
    material = [policy, RULE_VERSION, corpus()[0], settings.deepseek_base_url, settings.deepseek_model,
                settings.deepseek_effort, settings.embed_model, settings.rerank_model,
                state, [asdict(bar) for bar in bars]]
    return sha256(json.dumps(material, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


ReviewInput = tuple[dict[str, Any], tuple[Bar, ...]]


def _same_rule_after_warmup(old: dict[str, Any], current: dict[str, Any]) -> bool:
    """The rolling tape can change the hourly EMA seed, not the decision."""
    if old == current:
        return True
    old_facts, facts = old.get("facts", {}).copy(), current.get("facts", {}).copy()
    old_ema, ema = old_facts.pop("higher_ema20", None), facts.pop("higher_ema20", None)
    old_slope, slope = old_facts.pop("higher_ema_delta_atr", None), facts.pop("higher_ema_delta_atr", None)
    close, atr = facts.get("higher_close"), facts.get("atr14")
    if any(x is None for x in (old_ema, ema, old_slope, slope, close, atr)) or atr <= 0:
        return False
    # An hourly EMA seed can drift slightly when the left edge of the tape
    # moves. Bound price drift by 1% of the (unchanged) signal ATR, and don't
    # reuse across a change of EMA side or the original ±0.10 slope boundaries.
    def band(x: float) -> tuple[bool, bool, bool]:
        return x <= -0.1, x >= 0.1, x >= 0

    return ({**old, "facts": old_facts} == {**current, "facts": facts}
            and isclose(old_ema, ema, rel_tol=1e-5, abs_tol=0.01 * atr)
            and isclose(old_slope, slope, rel_tol=0, abs_tol=0.002)
            and (close > old_ema, close < old_ema) == (close > ema, close < ema)
            and band(old_slope) == band(slope))


class ReviewBusy(RuntimeError):
    pass


class ReviewAttemptChanged(RuntimeError):
    pass


class ReviewLedger:
    """Only valid verdicts enter replay. Failed provider attempts keep their evidence separately."""

    def __init__(self, path: Path, *, parallelism: int = 1, manual_reserve: int = 0) -> None:
        self.path = path
        self._parallelism = parallelism
        self._manual_reserve = manual_reserve
        self._slots = Condition()
        self._active: set[int | str] = set()

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("CREATE TABLE IF NOT EXISTS reviews (fingerprint TEXT PRIMARY KEY, result TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS attempts (fingerprint TEXT PRIMARY KEY, close_ms INTEGER NOT NULL, result TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS attempts_close_ms ON attempts(close_ms)")
        except Exception:
            db.close()
            raise
        return db

    def get(self, keys: tuple[str, ...]) -> dict[str, dict[str, Any]]:
        if not keys or not self.path.exists():
            return {}
        placeholders = ",".join("?" for _ in keys)
        with closing(self._connect()) as db:
            rows = db.execute(f"SELECT fingerprint, result FROM reviews WHERE fingerprint IN ({placeholders})", keys)
            return {key: json.loads(result) for key, result in rows}

    def matching(self, inputs: dict[str, ReviewInput]) -> dict[str, dict[str, Any]]:
        """Read exact matches or prove compatibility with saved inputs; never rewrite history."""
        saved = self.get(tuple(inputs))
        missing = {rule["close_ms"]: (key, rule, bars) for key, (rule, bars) in inputs.items() if key not in saved}
        if not missing or not self.path.exists():
            return saved
        compatible: dict[str, dict[str, Any]] = {}
        ambiguous: set[str] = set()
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT fingerprint, result FROM reviews "
                "WHERE json_extract(result, '$.rule.close_ms') BETWEEN ? AND ? ORDER BY rowid DESC",
                (min(missing), max(missing)),
            )
            for original_key, result in rows:
                original = json.loads(result)
                prior = original["rule"]
                if match := missing.get(prior["close_ms"]):
                    key, rule, bars = match
                    # Verify exact OHLCV (including all 80 prior bars), model and policy
                    # with the saved rule. Only then consider the two bounded diagnostics.
                    if not _same_rule_after_warmup(prior, rule):
                        continue
                    if fingerprint(prior, bars) != original_key:
                        continue
                    slot = key
                    first = compatible.setdefault(slot, original)["commentary"]
                    decision = original["commentary"]
                    if (first["verdict"], first.get("paper_plan")) != (decision["verdict"], decision.get("paper_plan")):
                        ambiguous.add(slot)
        saved.update({key: result for key, result in compatible.items() if key not in ambiguous})
        return saved

    def last_attempt(self, key: str, review_input: ReviewInput) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        rule, bars = review_input
        with closing(self._connect()) as db:
            rows = db.execute("SELECT fingerprint, result FROM attempts WHERE close_ms = ? "
                              "ORDER BY (fingerprint = ?) DESC, rowid DESC", (rule["close_ms"], key))
            for stored_key, raw in rows:
                result = json.loads(raw)
                prior = result["rule"]
                if stored_key == key or (_same_rule_after_warmup(prior, rule) and fingerprint(prior, bars) == stored_key):
                    return result
        return None

    def in_progress(self, close_ms: int) -> bool:
        with self._slots:
            return close_ms in self._active

    def run(self, key: str, analyze: Callable[[], dict[str, Any]], *,
            review_input: ReviewInput | None = None, retry_attempt_id: str | None = None,
            queue: bool = False) -> dict[str, Any]:
        def saved_review() -> dict[str, Any] | None:
            if review_input is not None:
                return self.matching({key: review_input}).get(key)
            return self.get((key,)).get(key)

        if saved := saved_review():
            return saved
        if review_input is not None and retry_attempt_id is None:
            if previous := self.last_attempt(key, review_input):
                return previous
        # A candle close is used only for locking, never to match two decisions.
        slot = review_input[0]["close_ms"] if review_input is not None else key
        limit = self._parallelism if queue else self._parallelism + self._manual_reserve
        with self._slots:
            while len(self._active) >= limit or slot in self._active:
                if not queue:
                    raise ReviewBusy
                self._slots.wait()
            self._active.add(slot)
        try:
            # A compatible rolling-window decision can have landed while we
            # acquired the slot, even if its stored key is not byte-identical.
            if saved := saved_review():
                return saved
            previous = self.last_attempt(key, review_input) if review_input is not None else None
            if retry_attempt_id is not None and (previous is None or retry_attempt_id != previous["attempt_id"]):
                raise ReviewAttemptChanged
            if previous and retry_attempt_id is None:
                return previous
            result = analyze()
            decision = result.get("commentary") or {}
            if (result.get("retrieval_status") == "ok" and result.get("commentary_status") == "ok"
                    and decision.get("verdict") in {"approve", "wait", "reject"}):
                with closing(self._connect()) as db, db:
                    db.execute("INSERT OR IGNORE INTO reviews VALUES (?, ?)", (key, json.dumps(result, ensure_ascii=False)))
                    if previous:
                        db.execute("DELETE FROM attempts WHERE close_ms = ? AND json_extract(result, '$.attempt_id') = ?",
                                   (review_input[0]["close_ms"], previous["attempt_id"]))
                    row = db.execute("SELECT result FROM reviews WHERE fingerprint = ?", (key,)).fetchone()
                    return json.loads(row[0])
            if review_input is not None and result.get("model_called"):
                # New receipt on every failed provider attempt. A duplicated or
                # stale retry click cannot spend again with an older receipt.
                result = {**result, "attempt_id": uuid4().hex, "attempt_completed_ms": time_ns() // 1_000_000}
                with closing(self._connect()) as db, db:
                    if previous:
                        db.execute("DELETE FROM attempts WHERE close_ms = ? AND json_extract(result, '$.attempt_id') = ?",
                                   (review_input[0]["close_ms"], previous["attempt_id"]))
                    db.execute("INSERT OR REPLACE INTO attempts VALUES (?, ?, ?)",
                               (key, review_input[0]["close_ms"], json.dumps(result, ensure_ascii=False)))
            return result
        finally:
            with self._slots:
                self._active.remove(slot)
                self._slots.notify_all()


def verdict(saved: dict[str, Any] | None) -> str:
    return (saved.get("commentary") or {}).get("verdict", "pending") if saved else "pending"


class ModelSlots:
    """Bound a work stage; manual requests win the next free slot."""

    def __init__(self, capacity: int) -> None:
        self._capacity, self._active, self._manual_waiting = capacity, 0, 0
        self._cond = Condition()

    @contextmanager
    def take(self, *, manual: bool):
        with self._cond:
            self._manual_waiting += manual
            try:
                while self._active >= self._capacity or (not manual and self._manual_waiting):
                    self._cond.wait()
                self._active += 1
            finally:
                self._manual_waiting -= manual
        try:
            yield
        finally:
            with self._cond:
                self._active -= 1
                self._cond.notify_all()


retrieval_slots = ModelSlots(LOCAL_REVIEW_PARALLELISM)
model_slots = ModelSlots(CLOUD_REVIEW_PARALLELISM)
review_ledger = ReviewLedger(settings.project_root / "data" / "market" / "pa-agent-graph-entry-reviews.db",
                             parallelism=LOCAL_REVIEW_PARALLELISM, manual_reserve=1)
