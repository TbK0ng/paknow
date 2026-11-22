"""The BTC decision card is causal, finite, and explicit about continuous trading."""
from math import isfinite

from paknow.pa_decision import decision_card, timeframe_snapshot
from paknow.pa_rules import FOUR_HOUR, HOUR, STEP, Bar, four_hourly

START = 1_735_689_600_000


def bars(count: int, *, direction: int = 1, base: float = 1000.0, span: int = STEP,
         start: int = START) -> tuple[Bar, ...]:
    result = []
    for i in range(count):
        price = base + direction * i
        opening = result[-1].close if result else price
        result.append(Bar(start + i * span, opening, max(opening, price) + 0.8,
                          min(opening, price) - 0.7, price, 10))
    return tuple(result)


def test_four_hourly_starts_at_a_utc_boundary_and_drops_partial_tail():
    source = bars(16 * 3 + 7)
    grouped = four_hourly(source)
    assert len(grouped) == 3
    assert grouped[0].ts_open == START
    assert grouped[0].open == source[0].open
    assert grouped[0].close == source[15].close
    assert grouped[0].high == max(bar.high for bar in source[:16])
    assert grouped[0].low == min(bar.low for bar in source[:16])
    assert grouped[0].volume == sum(bar.volume for bar in source[:16])


def test_decision_card_is_causal_finite_and_continuous_market_only():
    execution = bars(82)
    middle = bars(26, direction=1, span=HOUR, start=START - 6 * HOUR)
    major = bars(26, direction=-1, span=FOUR_HOUR, start=START - 21 * FOUR_HOUR)
    state = {
        "facts": {"atr14": 1.0},
        "original_state": "long",
        "plan": {"side": "long", "entry": 1000.0, "stop": 995.0, "target": 1010.0, "gross_rr": 2.0},
    }
    card = decision_card(state, execution, middle, major)
    assert card["session"] == "continuous_24_7"
    assert card["gap_analysis"] == "not_applicable"
    assert card["timeframes"]["execution"]["label"] == "15m"
    assert card["timeframes"]["middle"]["label"] == "1h"
    assert card["timeframes"]["major"]["label"] == "4h"
    assert card["timeframes"]["major"]["close_ms"] <= execution[-1].ts_open + STEP
    assert card["alignment"]["candidate_major_conflict"] is True
    assert card["follow_through"]["status"] == "awaiting_next_closed_bar"
    assert card["follow_through"]["close_above"] == execution[-2].high
    assert card["follow_through"]["close_below"] == execution[-2].low
    assert all(isfinite(card["timeframes"][key]["ema_delta_atr"])
               for key in ("execution", "middle", "major"))
    assert "跳空" in card["notes"][0]


def test_timeframe_snapshot_uses_a_fixed_window_when_older_history_appears():
    recent = bars(25)
    older = bars(10, start=START - 10 * FOUR_HOUR)
    assert timeframe_snapshot(recent, "4h", FOUR_HOUR) == timeframe_snapshot(older + recent, "4h", FOUR_HOUR)
