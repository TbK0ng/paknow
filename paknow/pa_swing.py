"""First-layer PA outline: structural validity, plus measurements for PAknow review."""
from __future__ import annotations

from typing import Any

VERSION = "swing-v3"
ONE_SIDE_COST = 0.0007  # BTC-USDT perpetual simulation: 5 bps taker + 2 bps slippage, no funding


def screen(original: dict[str, Any]) -> dict[str, Any]:
    """Validate directional price structure; expose distance and net R/R, not thresholds."""
    direction = original["state"]
    if direction not in {"long", "short"}:
        status = "unavailable" if direction == "unavailable" else "no_trigger"
        return {"version": VERSION, "status": status, "checks": [], "net_rr": None}

    levels = original[direction]["levels"]
    entry, stop, target = (levels[key] for key in ("reference", "invalidation", "target"))
    risk = abs(entry - stop)
    reward = abs(target - entry)
    sign = 1 if direction == "long" else -1
    bracket = sign * (entry - stop) > 0 and sign * (target - entry) > 0 and min(entry, stop, target) > 0
    net_rr = (reward - (entry + target) * ONE_SIDE_COST) / (risk + (entry + stop) * ONE_SIDE_COST) if bracket else None
    facts = original["facts"]
    ema_gap_atr = abs(entry - facts["ema20"]) / facts["atr14"]
    checks = [{"id": "bracket", "label": "PA 入场、止损、目标的价格方向有效", "passed": bracket}]
    return {"version": VERSION, "status": "pending" if bracket else "blocked",
            "checks": checks, "net_rr": net_rr, "ema_gap_atr": ema_gap_atr}
