"""Causal 80-bar market structure and its implications for an original-rule setup."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Sequence

from paknow.pa_rules import STEP, Bar

LOOKBACK = 80


@dataclass(frozen=True, slots=True)
class Background:
    count: int
    start_ms: int
    end_ms: int
    high: float
    low: float
    last_close: float
    change_pct: float
    efficiency: float
    structure: str
    regime: str


def summarize(prior: Sequence[Bar]) -> Background:
    """Read the 80 closes before the signal. The market adapter validates cadence."""
    if len(prior) != LOOKBACK:
        raise ValueError("背景必须恰好包含信号之前的 80 根 K 线")
    first, last = prior[0], prior[-1]
    high, low = max(b.high for b in prior), min(b.low for b in prior)
    move = last.close - first.close
    path = sum(abs(b.close - a.close) for a, b in zip(prior, prior[1:]))
    efficiency = abs(move) / path if path else 0.0
    early, recent = prior[:40], prior[40:]
    dh = max(b.high for b in recent) - max(b.high for b in early)
    dl = min(b.low for b in recent) - min(b.low for b in early)
    structure = "rising" if dh > 0 and dl > 0 else "falling" if dh < 0 and dl < 0 else "mixed"
    # An explicit display heuristic, not a new PA Original eligibility rule.
    directional = high > low and abs(move) >= 0.3 * (high - low) and efficiency >= 0.25
    regime = "up" if directional and move > 0 and structure == "rising" else (
        "down" if directional and move < 0 and structure == "falling" else "range_or_mixed"
    )
    return Background(LOOKBACK, first.ts_open, last.ts_open + STEP, high, low, last.close,
                      100 * move / first.close, efficiency, structure, regime)


def describe(background: Background, rule: dict[str, Any]) -> dict[str, Any]:
    """Add context and pre-entry planning, without rewriting the PA rule or the replay fills."""
    state = rule["state"]
    position = None
    if rule.get("facts"):
        close = rule["facts"]["close"]
        span = background.high - background.low
        position = (close - background.low) / span if span else None
    context = {**asdict(background), "signal_range_position": position}
    if state not in {"long", "short"}:
        return {"background": context, "plan": None}
    levels = rule[state]["levels"]
    sign = 1 if state == "long" else -1
    regime = background.regime
    alignment = "mixed" if regime == "range_or_mixed" else "aligned" if (sign > 0) == (regime == "up") else "counter"
    entry, stop, target = levels["reference"], levels["invalidation"], levels["target"]
    edge = background.high if sign > 0 else background.low
    risk = abs(entry - stop)
    return {"background": context, "plan": {
        "side": state, "alignment": alignment, "entry": entry, "stop": stop, "target": target,
        "gross_rr": abs(target - entry) / risk if risk else None,
        "edge_before_target": edge if 0 < sign * (edge - entry) < sign * (target - entry) else None,
    }}
