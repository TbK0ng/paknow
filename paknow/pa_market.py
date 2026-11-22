"""Public, confirmed OKX candles for PA Agent; snapshots are market data, not RAG data."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from paknow.config import settings
from paknow.pa_background import LOOKBACK, describe, summarize
from paknow.pa_decision import decision_card
from paknow.pa_plan import PaperPlan
from paknow.pa_replay import simulate
from paknow.pa_review import fingerprint, verdict
from paknow.pa_rules import FOUR_HOUR, HOUR, STEP, Bar, diagnose, four_hourly, hourly
from paknow.pa_swing import screen

SYMBOL = "BTC-USDT-SWAP"
BARS = 480
WARMUP = 420
MIN_WARMUP = 232
_ENDPOINT = "https://www.okx.com/api/v5/market/history-candles"
_TAPE_SIZE = BARS + WARMUP + 4


@dataclass(frozen=True)
class Market:
    tape: tuple[Bar, ...]
    fetched_ms: int
    states: tuple[dict[str, Any], ...]
    review_keys: tuple[str | None, ...]

    @property
    def bars(self) -> tuple[Bar, ...]:
        return self.tape[-BARS:]

    @property
    def last_ms(self) -> int:
        return self.tape[-1].ts_open + STEP

    def locate(self, close_ms: int) -> int:
        delta = close_ms - (self.bars[0].ts_open + STEP)
        index, remainder = divmod(delta, STEP)
        if remainder or not 0 <= index < BARS:
            raise LookupError("所选 K 线已不在当前窗口，请更新行情后重试。")
        return index

    def at(self, close_ms: int) -> tuple[dict[str, Any], tuple[Bar, ...]]:
        index = self.locate(close_ms)
        end = len(self.tape) - BARS + index + 1
        return self.states[index], self.tape[end - LOOKBACK - 1:end]

    def payload(self, *, stale: bool, reviews: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        replay = self.review_projection(reviews or {})
        return {
            "symbol": SYMBOL, "source": "OKX 公共行情", "stale": stale,
            "fetched_ms": self.fetched_ms, "first_ms": self.bars[0].ts_open,
            "last_ms": self.last_ms,
            **replay,
            "background_count": LOOKBACK,
            "prelude": [asdict(bar) for bar in self.tape[-BARS - LOOKBACK:-BARS]],
            "bars": [
                {**asdict(bar), "state": obs["state"], "swing_status": obs["swing"]["status"],
                 "ema20": obs.get("facts", {}).get("ema20")}
                for bar, obs in zip(self.bars, self.states)
            ],
        }
    def review_projection(self, reviews: dict[str, dict[str, Any]]) -> dict[str, Any]:
        statuses = [verdict(reviews.get(key)) if key else None for key in self.review_keys]
        plans: dict[int, PaperPlan] = {}
        for index, key in enumerate(self.review_keys):
            saved = reviews.get(key) if key else None
            if verdict(saved) == "approve":
                plan = PaperPlan.model_validate(saved["commentary"]["paper_plan"])
                plan.validate_signal(self.states[index]["original_state"], self.bars[index].close)
                plans[index] = plan
        orders, trades = simulate(self.bars, plans)
        return {"reviews": statuses, "orders": [asdict(order) for order in orders],
                "trades": [asdict(trade) for trade in trades]}


def assemble(bars: list[Bar], fetched_ms: int) -> Market:
    tape = tuple(sorted(bars, key=lambda bar: bar.ts_open)[-_TAPE_SIZE:])
    start = next((i for i, b in enumerate(tape) if b.ts_open % HOUR == 0), len(tape))
    tape = tape[start:]
    if len(tape) < BARS + MIN_WARMUP or not all(b.valid() for b in tape):
        raise ValueError("已收盘行情不足或 OHLCV 无效")
    if any(b.ts_open - a.ts_open != STEP for a, b in zip(tape, tape[1:])):
        raise ValueError("行情有缺口，不能用合成数据补齐")
    if tape[-1].ts_open + STEP > fetched_ms or tape[-1].ts_open + 3 * STEP < fetched_ms:
        raise ValueError("行情时间与下载时点不相符")
    hours = hourly(tape)
    four_hours = four_hourly(tape)
    first = len(tape) - BARS
    def analyze_close(end: int) -> dict[str, Any]:
        background = summarize(tape[end - LOOKBACK - 1:end - 1])
        selected_hours = hours[:end // 4]
        rule = diagnose(tape[:end], selected_hours)
        context = describe(background, rule)
        swing = screen(rule)
        original = rule["state"]
        close_ms = tape[end - 1].ts_open + STEP
        selected_four_hours = tuple(bar for bar in four_hours if bar.ts_open + FOUR_HOUR <= close_ms)
        state = {**rule, **context, "original_state": original, "swing": swing}
        return {**state, "decision": decision_card(state, tape[:end], selected_hours, selected_four_hours),
                "state": "wait" if swing["status"] == "blocked" else original}

    states = tuple(analyze_close(i) for i in range(first + 1, len(tape) + 1))
    keys = tuple(fingerprint(rule, tape[end - LOOKBACK - 1:end]) if rule["swing"]["status"] == "pending" else None
                 for end, rule in enumerate(states, first + 1))
    return Market(tape, fetched_ms, states, keys)


def download(client: httpx.Client, now_ms: int) -> Market:
    current_open = now_ms // STEP * STEP
    after = current_open
    confirmed: dict[int, Bar] = {}
    for _ in range(10):
        response = client.get(_ENDPOINT, params={"instId": SYMBOL, "bar": "15m", "limit": 100, "after": after})
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != "0" or not payload.get("data"):
            raise ValueError("OKX 未返回公开历史行情")
        rows = payload["data"]
        oldest = min(int(row[0]) for row in rows)
        if oldest >= after:
            raise ValueError("OKX 分页没有前进")
        for row in rows:
            if len(row) < 9 or row[8] != "1" or (ts := int(row[0])) >= current_open:
                continue
            bar = Bar(ts, *(float(value) for value in row[1:6]))
            if not bar.valid():
                raise ValueError("OKX 返回无效 OHLCV")
            confirmed.setdefault(ts, bar)
        if len(confirmed) >= _TAPE_SIZE:
            break
        after = oldest
    if max(confirmed, default=0) not in {current_open - STEP, current_open - 2 * STEP}:
        raise ValueError("OKX 未返回近期已收盘行情")
    return assemble(list(confirmed.values()), now_ms)


class PublicMarket:
    """Serialize refreshes; only use a previously validated real snapshot on I/O failure."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / "pa-agent-btc-usdt-swap-15m.json"
        self.lock = threading.Lock()
        self.current: Market | None = None
        self.last_attempt: float | None = None
        self.stale = False

    def get(self, *, refresh: bool = False) -> tuple[Market, bool]:
        with self.lock:
            if not refresh and self.current is not None:
                age = time.time_ns() // 1_000_000 - self.current.last_ms
                return self.current, self.stale or age >= STEP * 2
            if self.last_attempt is not None and time.monotonic() - self.last_attempt < 35 and self.current:
                return self.current, self.stale
            self.last_attempt = time.monotonic()
            try:
                with httpx.Client(timeout=6, follow_redirects=False) as client:
                    self.current = download(client, time.time_ns() // 1_000_000)
            except (httpx.HTTPError, ValueError, TypeError, IndexError, KeyError):
                self.stale = True
                self.current = self.current or self._read_saved()
                if self.current is None:
                    raise LookupError("无法下载 OKX 行情，且无已验证的历史快照。") from None
            else:
                self.stale = False
                try:
                    self._save(self.current)
                except OSError:  # disk cache is optional; usable live data stays in memory
                    pass
            return self.current, self.stale

    def _saved_markets(self, archive_limit: int | None):
        for path in [self.path, *sorted(self.path.parent.glob("btc-usdt-swap-15m-*-okx.json"), reverse=True)[:archive_limit]]:
            try:
                if path.stat().st_size > 8 * 1024 * 1024:
                    continue
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved.get("symbol") != SYMBOL or saved.get("source") != "okx":
                    continue
                bars = [Bar(**{key: row[key] for key in Bar.__slots__}) for row in saved["bars"] if row.get("closed") is True]
                yield assemble(bars, saved["downloaded_at_ms"])
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                continue

    def _read_saved(self) -> Market | None:
        return next(self._saved_markets(2), None)

    def history(self, close_ms: int) -> Market:
        """Find a real replay window around an archived signal, without downloading."""
        signal = close_ms - STEP
        by_open: dict[int, Bar] = {}
        for snapshot in self._saved_markets(None):
            for bar in snapshot.tape:
                by_open.setdefault(bar.ts_open, bar)
        if signal not in by_open:
            raise LookupError("本地没有这笔记录对应的已收盘行情，无法跳转。")
        # Keep the signal in the visible range while leaving enough room
        # for warmup at the left edge. Never bridge gaps between archives.
        end = signal
        while end + STEP in by_open and end < signal + (BARS - 1) * STEP:
            end += STEP
        run = []
        ts = end
        while ts in by_open and len(run) < _TAPE_SIZE:
            run.append(by_open[ts])
            ts -= STEP
        try:
            market = assemble(run, end + STEP)
            market.locate(close_ms)
        except (ValueError, LookupError) as exc:
            raise LookupError("本地连续行情不足以回放这笔记录。") from exc
        return market

    def _save(self, market: Market) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"source": "okx", "symbol": SYMBOL, "downloaded_at_ms": market.fetched_ms,
                                   "bars": [{**asdict(b), "closed": True} for b in market.tape]}), encoding="utf-8")
        temp.replace(self.path)


market_feed = PublicMarket(settings.project_root / "data" / "market")
