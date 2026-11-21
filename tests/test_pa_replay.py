"""Causal fills for independent PAknow paper plans."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from paknow.pa_plan import PaperPlan
from paknow.pa_replay import simulate
from paknow.pa_rules import STEP, Bar


def candles(*prices: tuple[int, int, int, int]) -> tuple[Bar, ...]:
    return tuple(Bar(1_735_689_600_000 + STEP * i, o, h, l, c, 10) for i, (o, h, l, c) in enumerate(prices))


def plan(**overrides) -> PaperPlan:
    return PaperPlan.model_validate({"side": "long", "entry_mode": "next_open", "entry_level": None,
                                     "entry_window_bars": 1, "stop_price": 90, "target_price": 110,
                                     "exit_on_close": None, "max_hold_bars": 12, **overrides})


def test_overlapping_trades_ignore_signal_bar_and_run_independently():
    bars = candles((100, 119, 80, 100), (100, 103, 97, 100), (100, 110, 97, 105), (105, 106, 93, 96))
    orders, (long, short) = simulate(bars, {0: plan(), 1: plan(side="short", stop_price=114, target_price=93)})
    assert [o.status for o in orders] == ["filled", "filled"]
    assert (long.entry_index, long.exit_index, long.exit_price, long.pnl_pct) == (1, 2, 110, 10)
    assert (short.entry_index, short.exit_index, short.exit_price) == (2, 3, 93)
    assert short.pnl_pct == pytest.approx(7)


@pytest.mark.parametrize(("side", "stop", "target", "ohlc", "reason", "fill"), [
    ("long", 95, 105, (100, 106, 94, 101), "ambiguous_stop", 95),
    ("short", 105, 95, (100, 105, 95, 101), "ambiguous_stop", 105),
    ("long", 95, 105, (92, 110, 90, 100), "stop", 92),
    ("short", 105, 95, (108, 110, 90, 100), "stop", 108),
    ("long", 95, 105, (108, 110, 93, 100), "target", 105),
    ("short", 105, 95, (93, 110, 92, 100), "target", 95),
])
def test_open_positions_prioritize_known_gap_and_conservative_overlap(side, stop, target, ohlc, reason, fill):
    bars = candles((100, 103, 98, 100), (100, 103, 98, 100), ohlc)
    _, (trade,) = simulate(bars, {0: plan(side=side, stop_price=stop, target_price=target)})
    assert (trade.exit_index, trade.exit_reason, trade.exit_price) == (2, reason, fill)


def test_close_confirmation_waits_for_next_open_and_cannot_fill_after_expiry():
    bars = candles((100, 107, 96, 100), (100, 109, 92, 105), (106, 108, 104, 106))
    confirmation = plan(entry_mode="close_confirm", entry_level=104, entry_window_bars=2)
    _, (trade,) = simulate(bars, {0: confirmation})
    assert (trade.entry_index, trade.entry_price, trade.exit_index) == (2, 106, None)
    (order,), none = simulate(bars, {0: confirmation.model_copy(update={"entry_window_bars": 1})})
    assert not none and (order.status, order.result_index) == ("expired", 1)


@pytest.mark.parametrize("mode,level", [("close_confirm", 104), ("pullback_confirm", 97)])
def test_confirmation_schema_requires_time_for_a_next_open(mode, level):
    with pytest.raises(ValidationError, match="at least two candles"):
        plan(entry_mode=mode, entry_level=level, entry_window_bars=1)
    assert plan(entry_mode=mode, entry_level=level, entry_window_bars=2).entry_window_bars == 2


@pytest.mark.parametrize("mode,level,expected_status", [("pullback_touch", 97, "filled"), ("breakout_touch", 108, "expired")])
def test_price_touch_and_no_trade_if_not_triggered(mode, level, expected_status):
    bars = candles((100, 120, 85, 100), (100, 102, 97, 101), (101, 103, 99, 100))
    candidate = plan(entry_mode=mode, entry_level=level, entry_window_bars=2)
    (order,), trades = simulate(bars, {0: candidate})
    assert order.status == expected_status
    if trades:
        assert trades[0].entry_index == 1 and trades[0].entry_price == 97 and trades[0].exit_price is None


def test_pullback_confirmation_requires_reclaim_not_touch():
    bars = candles((100, 103, 98, 100), (100, 102, 96, 98), (95, 99, 94, 98), (98, 101, 97, 99))
    candidate = plan(entry_mode="pullback_confirm", entry_level=97, entry_window_bars=3)
    _, (trade,) = simulate(bars, {0: candidate})
    assert (trade.entry_index, trade.entry_price) == (3, 98)


def test_uncertain_intrabar_sequence_does_not_credit_target_but_may_stop():
    b = candles((100, 102, 98, 100), (100, 111, 94, 104), (104, 106, 102, 104))
    c = plan(entry_mode="breakout_touch", entry_level=105, entry_window_bars=3, stop_price=95)
    _, (trade,) = simulate(b, {0: c})
    assert (trade.entry_index, trade.exit_index, trade.exit_reason, trade.exit_price) == (1, 1, "ambiguous_entry_stop", 95)
    _, (without_stop,) = simulate(b, {0: c.model_copy(update={"stop_price": 90})})
    assert without_stop.exit_price is None


def test_structural_close_exit_and_max_hold_use_explicit_timing():
    bars = candles((100, 102, 98, 100), (100, 102, 97, 98), (97, 101, 96, 101))
    _, (structure,) = simulate(bars, {0: plan(exit_on_close=99)})
    assert (structure.exit_reason, structure.exit_price, structure.exit_index) == ("close_invalidation", 97, 2)
    _, (timeout,) = simulate(bars, {0: plan(max_hold_bars=1)})
    assert (timeout.exit_reason, timeout.exit_price, timeout.exit_index) == ("time_exit", 98, 1)


def test_bad_geometry_no_trigger_and_gap_do_not_create_real_or_fake_fills():
    with pytest.raises(ValidationError):
        plan(stop_price=110, target_price=90)
    with pytest.raises(ValueError):
        plan(entry_mode="pullback_touch", entry_level=101).validate_signal("long", 100)
    b = candles((100, 102, 98, 100), (93, 97, 92, 97))
    (order,), trades = simulate(b, {0: plan(stop_price=95)})
    assert order.status == "invalidated" and not trades
    (unresolved,), trades = simulate(b[:1], {0: plan()})
    assert unresolved.status == "armed" and not trades
