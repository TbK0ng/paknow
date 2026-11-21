# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

STEP = 900_000
HOUR = 4 * STEP
VERSION = "2026-09-v1"


@dataclass(frozen=True, slots=True)
class Bar:
    ts_open: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def valid(self) -> bool:
        return (
            self.ts_open > 0 and self.ts_open % STEP == 0
            and all(isfinite(x) and x > 0 for x in (self.open, self.high, self.low, self.close))
            and self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high
            and isfinite(self.volume) and self.volume >= 0
        )


def hourly(bars: tuple[Bar, ...]) -> tuple[Bar, ...]:
    """Caller validates gap-free tape starting on an hour. Drop its partial last hour."""
    return tuple(
        Bar(group[0].ts_open, group[0].open, max(b.high for b in group), min(b.low for b in group),
            group[-1].close, sum(b.volume for b in group))
        for group in (bars[i:i + 4] for i in range(0, len(bars) - 3, 4))
    )


@dataclass(frozen=True, slots=True)
class Frame:
    bars: tuple[Bar, ...]  # newest first
    ema: tuple[float, ...]
    atr: tuple[float, ...]


def _frame(bars: tuple[Bar, ...], keep: int) -> Frame:
    bars = bars[-(keep + 180):]
    closes = [bar.close for bar in bars]
    tr = [bars[0].high - bars[0].low] + [
        max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close)) for a, b in zip(bars, bars[1:])
    ]
    alpha = 2 / 21
    ema = [float("nan")] * 19 + [sum(closes[:20]) / 20]
    for close in closes[20:]:
        ema.append(close * alpha + ema[-1] * (1 - alpha))
    atr = [float("nan")] * 13 + [sum(tr[:14]) / 14]
    for value in tr[14:]:
        atr.append((atr[-1] * 13 + value) / 14)
    return Frame(bars[-keep:][::-1], tuple(ema[-keep:][::-1]), tuple(atr[-keep:][::-1]))


def _trend(frame: Frame, sign: int) -> bool:
    return sign * (frame.bars[0].close - frame.ema[0]) > 0 and sign * (frame.ema[0] - frame.ema[5]) / frame.atr[0] >= 0.1


def _pivot(frame: Frame, close: float, sign: int, minimum: float) -> float | None:
    levels = []
    for i in range(2, len(frame.bars) - 1):
        newer, bar, older = frame.bars[i - 1:i + 2]
        if sign > 0 and bar.high > newer.high and bar.high >= older.high:
            price = bar.high
        elif sign < 0 and bar.low < newer.low and bar.low <= older.low:
            price = bar.low
        else:
            continue
        if sign * (price - close) >= minimum:
            levels.append(price)
    return min(levels, key=lambda price: abs(price - close), default=None)


def _inspect(frame: Frame, higher: Frame, sign: int) -> dict[str, Any]:
    b, ema, atr = frame.bars, frame.ema, frame.atr[0]
    now, prev = b[:2]
    close = now.close
    if sign > 0:
        confirmation = close > now.open and (close > prev.high or (now.high > prev.high and close >= prev.close))
        touches = [j + 1 for j in range(1, 7) if b[j].low <= ema[j] + 0.8 * atr]
        tests = [j + 1 for j in range(2, 8) if b[j].low < b[j + 1].low and prev.low <= b[j].low + 0.6 * atr]
    else:
        confirmation = close < now.open and (close < prev.low or (now.low < prev.low and close <= prev.close))
        touches = [j + 1 for j in range(1, 7) if b[j].high >= ema[j] - 0.8 * atr]
        tests = [j + 1 for j in range(2, 8) if b[j].high > b[j + 1].high and prev.high >= b[j].high - 0.6 * atr]
    ema_fallback = abs(prev.close - ema[1]) <= 1.2 * atr
    local = _trend(frame, sign)
    counter = _trend(higher, -sign)
    checks = [
        {"id": "trigger", "label": "同向收盘确认", "passed": confirmation},
        {"id": "local_trend", "label": "15 分钟趋势支持方向", "passed": local},
        {"id": "higher_trend", "label": "1 小时未出现强反向", "passed": not counter},
        {"id": "pullback", "label": "近六根回踩靠近 EMA20", "passed": bool(touches)},
        {"id": "second_test", "label": "二次测试或 EMA 代理条件", "passed": bool(tests) or ema_fallback},
    ]
    levels = None
    if all(check["passed"] for check in checks):
        raw = min(x.low for x in b[:4]) if sign > 0 else max(x.high for x in b[:4])
        risk = min(2.2 * atr, max(0.8 * atr, sign * (close - raw)))
        invalidation = close - sign * risk
        minimum = max((abs(close - invalidation) + 0.25 * atr) * 1.55, 2.5 * atr)
        bound_15, bound_1h = _pivot(frame, close, sign, 1.8 * atr), _pivot(higher, close, sign, 1.8 * atr)
        bound = bound_15 if bound_15 is not None else bound_1h
        origin = "15m 结构" if bound_15 is not None else "1h 结构"
        if bound is None or sign * (bound - close) < minimum:
            bound = _pivot(frame, close, sign, minimum)
            origin = "15m 结构"
            if bound is None:
                bound, origin = _pivot(higher, close, sign, minimum), "1h 结构"
            if bound is None:
                bound, origin = close + sign * (minimum + 0.5 * atr), "ATR 规则估算"
        levels = {"reference": close, "invalidation": invalidation, "target": bound - sign * 0.02 * atr, "target_basis": origin}
    return {"checks": checks, "touch_bars": touches, "test_bars": tests, "ema_proxy": ema_fallback and not tests, "levels": levels}


def diagnose(bars: tuple[Bar, ...], hours: tuple[Bar, ...]) -> dict[str, Any]:
    """Evaluate only complete candles at the selected 15m close, never AI or orders."""
    if len(bars) < 199 or len(hours) < 25:
        raise ValueError("策略需要完整的 15 分钟和 1 小时预热")
    now = bars[-1]
    if not 0 <= now.ts_open + STEP - (hours[-1].ts_open + HOUR) < HOUR:
        raise ValueError("1 小时数据和所选收盘时间不一致")
    frame, higher = _frame(bars, 50), _frame(hours, 20)
    if not all(value > 0 and isfinite(value) for series in (frame.ema, frame.atr, higher.ema, higher.atr) for value in series[:6]):
        return {"state": "unavailable", "reason": "指标不足或无波动"}
    long, short = _inspect(frame, higher, 1), _inspect(frame, higher, -1)
    return {"state": "long" if long["levels"] else "short" if short["levels"] else "wait", "long": long, "short": short,
            "close_ms": now.ts_open + STEP, "higher_close_ms": higher.bars[0].ts_open + HOUR,
            "facts": {"open": now.open, "high": now.high, "low": now.low, "close": now.close, "volume": now.volume,
                      "previous_high": frame.bars[1].high, "previous_low": frame.bars[1].low,
                      "ema20": frame.ema[0], "atr14": frame.atr[0], "ema_delta_atr": (frame.ema[0] - frame.ema[5]) / frame.atr[0],
                      "higher_close": higher.bars[0].close, "higher_ema20": higher.ema[0],
                      "higher_ema_delta_atr": (higher.ema[0] - higher.ema[5]) / higher.atr[0]}}
