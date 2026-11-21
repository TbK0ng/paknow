"""Replay validated PAknow paper plans against later, confirmed OHLC only."""
from __future__ import annotations

from dataclasses import dataclass, replace

from paknow.pa_plan import PaperPlan
from paknow.pa_rules import Bar


@dataclass(frozen=True, slots=True)
class ReplayTrade:
    side: str
    signal_index: int
    entry_index: int
    entry_price: float
    stop_price: float
    target_price: float
    exit_index: int | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    pnl_pct: float | None = None


@dataclass(frozen=True, slots=True)
class ReplayOrder:
    signal_index: int
    status: str
    result_index: int | None = None


def _exit(trade: ReplayTrade, bar: Bar) -> tuple[str, float] | None:
    stop, target = trade.stop_price, trade.target_price
    if trade.side == "long":
        if bar.open <= stop:
            return "stop", bar.open
        if bar.open >= target:
            return "target", target
        hit_stop, hit_target = bar.low <= stop, bar.high >= target
    else:
        if bar.open >= stop:
            return "stop", bar.open
        if bar.open <= target:
            return "target", target
        hit_stop, hit_target = bar.high >= stop, bar.low <= target
    if hit_stop:
        return ("ambiguous_stop" if hit_target else "stop"), stop
    return ("target", target) if hit_target else None


def _closed(trade: ReplayTrade, index: int, reason: str, price: float) -> ReplayTrade:
    sign = 1 if trade.side == "long" else -1
    return replace(trade, exit_index=index, exit_price=price, exit_reason=reason,
                   pnl_pct=sign * (price - trade.entry_price) / trade.entry_price * 100)


def _manage(bars: tuple[Bar, ...], trade: ReplayTrade, plan: PaperPlan, *, intrabar: bool) -> ReplayTrade:
    early_exit = False
    sign = 1 if plan.side == "long" else -1
    for i in range(trade.entry_index, len(bars)):
        bar = bars[i]
        if early_exit and plan.contains(bar.open):
            return _closed(trade, i, "close_invalidation", bar.open)
        if i == trade.entry_index and intrabar:
            # The trigger and extremes have no known order within one candle.
            # Do not credit a same-bar target; possible stop wins conservatively.
            extreme = bar.low if sign > 0 else bar.high
            result = ("ambiguous_entry_stop", plan.stop_price) if sign * (extreme - plan.stop_price) <= 0 else None
        else:
            result = _exit(trade, bar)
        if result:
            return _closed(trade, i, *result)
        if i - trade.entry_index + 1 >= plan.max_hold_bars:
            # A time exit on the trigger candle must not credit a price beyond TP.
            price = min(bar.close, plan.target_price) if sign > 0 else max(bar.close, plan.target_price)
            return _closed(trade, i, "time_exit", price)
        early_exit = plan.exit_on_close is not None and sign * (bar.close - plan.exit_on_close) <= 0
    return trade


def _run(bars: tuple[Bar, ...], index: int, plan: PaperPlan) -> tuple[ReplayOrder, ReplayTrade | None]:
    if index < 0 or index >= len(bars):
        raise ValueError("paper plan signal is outside the replay window")
    sign = 1 if plan.side == "long" else -1
    last = index + plan.entry_window_bars
    open_next = plan.entry_mode == "next_open"
    for j in range(index + 1, min(last + 1, len(bars))):
        bar = bars[j]
        if not plan.contains(bar.open):
            return ReplayOrder(index, "invalidated", j), None
        stopped = (bar.low <= plan.stop_price if sign > 0 else bar.high >= plan.stop_price)
        level = plan.entry_level
        if open_next:
            fill, intrabar = bar.open, False
        elif plan.entry_mode in {"close_confirm", "pullback_confirm"}:
            consumed = (bar.high >= plan.target_price if sign > 0 else bar.low <= plan.target_price)
            if stopped or consumed:
                return ReplayOrder(index, "invalidated", j), None
            open_next = sign * (bar.close - level) >= 0 and plan.contains(bar.close)
            if plan.entry_mode == "pullback_confirm":
                touched = bar.low <= level if sign > 0 else bar.high >= level
                open_next = open_next and touched and sign * (bar.close - bar.open) > 0
            continue
        else:
            pullback = plan.entry_mode == "pullback_touch"
            trigger_sign = -sign if pullback else sign
            extreme = bar.high if trigger_sign > 0 else bar.low
            if trigger_sign * (bar.open - level) >= 0:
                fill, intrabar = bar.open, False
            elif trigger_sign * (extreme - level) >= 0:
                fill, intrabar = level, True
            elif stopped:
                return ReplayOrder(index, "invalidated", j), None
            else:
                continue
        trade = ReplayTrade(plan.side, index, j, fill, plan.stop_price, plan.target_price)
        return ReplayOrder(index, "filled", j), _manage(bars, trade, plan, intrabar=intrabar)
    if last < len(bars):
        return ReplayOrder(index, "expired", last), None
    return ReplayOrder(index, "armed"), None


def simulate(bars: tuple[Bar, ...], plans: dict[int, PaperPlan]) -> tuple[tuple[ReplayOrder, ...], tuple[ReplayTrade, ...]]:
    """Independent paths by signal index; no cross-position interaction."""
    orders, trades = [], []
    for i, plan in sorted(plans.items()):
        order, trade = _run(bars, i, plan)
        orders.append(order)
        if trade:
            trades.append(trade)
    return tuple(orders), tuple(trades)
