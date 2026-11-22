"""Causal bar-by-bar decision card for the continuous BTC market.

The checklist is informed by Tom Hougaard's no-hindsight bar-by-bar method.
It describes completed data and conditional next-bar scenarios; it is not an
entry gate, a probability claim, or evidence for the live candidate.
"""
from __future__ import annotations

from math import isfinite
from statistics import median
from typing import Any

from paknow.pa_rules import FOUR_HOUR, HOUR, STEP, Bar, _frame

VERSION = "hougaard-btc-v1"
TIMEFRAME_BARS = 20


def _structure(bars: tuple[Bar, ...]) -> str:
    # Use a fixed recent window so adding older tape history cannot rewrite a
    # saved review's structure label.
    early, recent = bars[-TIMEFRAME_BARS:-TIMEFRAME_BARS // 2], bars[-TIMEFRAME_BARS // 2:]
    if not early or not recent:
        return "mixed"
    dh = max(bar.high for bar in recent) - max(bar.high for bar in early)
    dl = min(bar.low for bar in recent) - min(bar.low for bar in early)
    return "rising" if dh > 0 and dl > 0 else "falling" if dh < 0 and dl < 0 else "mixed"


def timeframe_snapshot(bars: tuple[Bar, ...], label: str, span: int) -> dict[str, Any]:
    """Describe one completed timeframe without looking at a forming candle."""
    if len(bars) < TIMEFRAME_BARS + 5:
        return {"label": label, "available": False, "reason": "完整K线不足"}
    # A fixed 25-bar window keeps the EMA/ATR seed stable when the left edge
    # of the rolling tape moves and older history becomes available.
    frame = _frame(bars[-(TIMEFRAME_BARS + 5):], TIMEFRAME_BARS)
    if not all(isfinite(value) and value > 0 for value in (frame.ema[0], frame.ema[5], frame.atr[0])):
        return {"label": label, "available": False, "reason": "指标不足或无波动"}
    close, ema, atr = frame.bars[0].close, frame.ema[0], frame.atr[0]
    slope = (frame.ema[0] - frame.ema[5]) / atr
    if slope >= 0.1 and close > ema:
        regime = "up"
    elif slope <= -0.1 and close < ema:
        regime = "down"
    else:
        regime = "range_or_mixed"
    recent = bars[-TIMEFRAME_BARS:]
    high, low = max(bar.high for bar in recent), min(bar.low for bar in recent)
    position = (close - low) / (high - low) if high > low else 0.5
    return {
        "label": label,
        "available": True,
        "regime": regime,
        "structure": _structure(bars),
        "close": close,
        "ema20": ema,
        "atr14": atr,
        "ema_delta_atr": slope,
        "range_position": position,
        "close_ms": bars[-1].ts_open + span,
    }


def _streak(bars: tuple[Bar, ...], *, above: bool) -> int:
    count = 0
    for previous, current in reversed(tuple(zip(bars, bars[1:]))):
        if (current.close > previous.high) if above else (current.close < previous.low):
            count += 1
        else:
            break
    return count


def _alignment(side: str, regime: str) -> str:
    if side not in {"long", "short"} or regime == "range_or_mixed":
        return "mixed"
    return "aligned" if (side == "long") == (regime == "up") else "counter"


def _bar_facts(bars: tuple[Bar, ...], atr: float) -> dict[str, Any]:
    signal, previous = bars[-1], bars[-2]
    span = signal.high - signal.low
    recent = [bar.high - bar.low for bar in bars[-21:-1]]
    typical = median(recent) if recent else span
    return {
        "direction": "bull" if signal.close > signal.open else "bear" if signal.close < signal.open else "flat",
        "body_atr": (signal.close - signal.open) / atr if atr else 0.0,
        "close_location": (signal.close - signal.low) / span if span else 0.5,
        "range_atr": span / atr if atr else 0.0,
        "range_vs_recent": span / typical if typical else None,
        "inside": signal.high <= previous.high and signal.low >= previous.low,
        "outside": signal.high > previous.high and signal.low < previous.low,
        "higher_high": signal.high > previous.high,
        "lower_low": signal.low < previous.low,
        "higher_low": signal.low > previous.low,
        "lower_high": signal.high < previous.high,
        "close_above_prior_high": signal.close > previous.high,
        "close_below_prior_low": signal.close < previous.low,
    }


def decision_card(state: dict[str, Any], bars: tuple[Bar, ...], hours: tuple[Bar, ...],
                  four_hours: tuple[Bar, ...]) -> dict[str, Any]:
    """Build a causal checklist from completed 15m, 1h and 4h bars."""
    signal_close = bars[-1].ts_open + STEP
    hours = tuple(bar for bar in hours if bar.ts_open + HOUR <= signal_close)
    four_hours = tuple(bar for bar in four_hours if bar.ts_open + FOUR_HOUR <= signal_close)
    execution = timeframe_snapshot(bars, "15m", STEP)
    middle = timeframe_snapshot(hours, "1h", HOUR)
    major = timeframe_snapshot(four_hours, "4h", FOUR_HOUR)
    atr = state.get("facts", {}).get("atr14", 0.0)
    facts = _bar_facts(bars, atr)
    previous = bars[-2]
    side = state.get("original_state", "wait")
    major_alignment = _alignment(side, major.get("regime", "range_or_mixed"))
    middle_alignment = _alignment(side, middle.get("regime", "range_or_mixed"))
    conflict = (middle.get("regime") in {"up", "down"} and major.get("regime") in {"up", "down"}
                and middle["regime"] != major["regime"])
    transition = (
        "middle_major_conflict" if conflict
        else "execution_trend_in_middle_range"
        if execution.get("regime") in {"up", "down"} and middle.get("regime") == "range_or_mixed"
        else "major_range"
        if major.get("regime") == "range_or_mixed"
        else "aligned_or_neutral"
    )
    plan = state.get("plan")
    risk = None
    if plan and plan.get("side") in {"long", "short"}:
        sign = 1 if plan["side"] == "long" else -1
        entry, stop, target = plan["entry"], plan["stop"], plan["target"]
        atr = state.get("facts", {}).get("atr14", 0.0)
        barrier = None
        if major.get("available"):
            recent_major = four_hours[-TIMEFRAME_BARS:]
            level = max(bar.high for bar in recent_major) if sign > 0 else min(bar.low for bar in recent_major)
            if 0 < sign * (level - entry) < sign * (target - entry):
                barrier = level
        risk = {
            "stop_atr": abs(entry - stop) / atr if atr else None,
            "target_atr": abs(target - entry) / atr if atr else None,
            "gross_rr": plan.get("gross_rr"),
            "major_barrier_before_target": barrier,
        }
    scenarios = [
        {"id": "up", "close_trigger": previous.high,
         "response": "只有后续收盘高于前一根高点，才形成向上的收盘跟进；盘中触及不算。"},
        {"id": "down", "close_trigger": previous.low,
         "response": "只有后续收盘低于前一根低点，才形成向下的收盘跟进；盘中触及不算。"},
        {"id": "inside", "close_trigger": None,
         "response": "若仍在前一根高低点之间，保留等待，不追认未完成的突破。"},
    ]
    return {
        "version": VERSION,
        "session": "continuous_24_7",
        "gap_analysis": "not_applicable",
        "timeframes": {"execution": execution, "middle": middle, "major": major},
        "alignment": {
            "middle": middle_alignment,
            "major": major_alignment,
            "candidate_major_conflict": major_alignment == "counter",
        },
        "transition": transition,
        "signal_bar": facts,
        "sequence": {
            "up_close_streak": _streak(bars[-5:], above=True),
            "down_close_streak": _streak(bars[-5:], above=False),
            "last_close_break": (
                "up" if facts["close_above_prior_high"]
                else "down" if facts["close_below_prior_low"] else "none"
            ),
        },
        "follow_through": {
            "status": "awaiting_next_closed_bar",
            "close_above": previous.high,
            "close_below": previous.low,
        },
        "scenarios": scenarios,
        "risk": risk,
        "notes": [
            "BTC 连续交易，不使用开盘跳空、缺口回补或跨股指联动作为价格条件。",
            "4 小时只提供大周期背景和冲突提示，不替代 15 分钟候选规则。",
        ],
    }
