"""Strict executable paper plans. Prices are proposed by PAknow, never by the browser."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Price = Annotated[float, Field(gt=0, allow_inf_nan=False, strict=True)]


class PaperPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    side: Literal["long", "short"]
    entry_mode: Literal["next_open", "breakout_touch", "pullback_touch", "close_confirm", "pullback_confirm"]
    entry_level: Price | None
    entry_window_bars: int = Field(ge=1, le=32)
    stop_price: Price
    target_price: Price
    exit_on_close: Price | None
    max_hold_bars: int = Field(ge=1, le=192)

    @model_validator(mode="after")
    def ordered_prices(self) -> PaperPlan:
        at_market = self.entry_mode == "next_open"
        if at_market != (self.entry_level is None) or (at_market and self.entry_window_bars != 1):
            raise ValueError("next_open has no trigger level and expires after one candle")
        if self.entry_mode in {"close_confirm", "pullback_confirm"} and self.entry_window_bars < 2:
            raise ValueError("confirmation needs at least two candles: confirmation close and next open")
        sign = 1 if self.side == "long" else -1
        if sign * (self.target_price - self.stop_price) <= 0:
            raise ValueError("stop and target are reversed")
        for value in (self.entry_level, self.exit_on_close):
            if value is not None and not 0 < sign * (value - self.stop_price) < sign * (self.target_price - self.stop_price):
                raise ValueError("entry and structural exit must be inside the bracket")
        return self

    def validate_signal(self, side: str, close: float) -> None:
        if self.side != side:
            raise ValueError("review may not reverse the original candidate side")
        sign = 1 if side == "long" else -1
        if self.entry_mode == "next_open":
            if not self.contains(close):
                raise ValueError("next-open proposal must bracket the signal close")
        elif self.entry_level is not None:
            delta = sign * (self.entry_level - close)
            pullback = self.entry_mode in {"pullback_touch", "pullback_confirm"}
            if (pullback and delta >= 0) or (not pullback and delta <= 0):
                raise ValueError("conditional trigger points in the wrong direction")

    def contains(self, price: float) -> bool:
        if self.side == "long":
            return self.stop_price < price < self.target_price
        return self.target_price < price < self.stop_price
