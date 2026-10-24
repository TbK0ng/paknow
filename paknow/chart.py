"""Structured chart read: the one pass that turns a screenshot into fields.

The read is not evidence (ADR-0002): it has no evidence blocks therefore cannot
produce ``[n]`` citations. It either returns a :class:`ChartRead` or a
classified failure, never raises, so a bad read degrades instead of breaking
the run.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ValidationError

from paknow.gateway import _strip_count, gateway

#: Total budget for the read call; the SDK timeout is the only deadline owner.
READ_TIMEOUT_S = 120


class Swing(BaseModel):
    kind: Literal["high", "low"]
    price: float | None  # any uncertain digit -> None, never extrapolated
    strip: int
    basis: str


class Level(BaseModel):
    price: float | None
    source: Literal["annotation", "swing", "axis", "unknown"]
    strip: int
    basis: str


class Focus(BaseModel):
    price: float
    why: str


class ChartRead(BaseModel):
    instrument: str | None  # not printed on the chart -> None
    timeframe: str | None
    market_state: Literal["trend_up", "trend_down", "trading_range", "unclear"]
    swings: list[Swing]
    levels: list[Level]  # every readable level, never trimmed
    focus: list[Focus]  # 5-8 suggestions
    patterns: list[str]
    annotations: list[str]  # chart text verbatim
    indicators: list[str]  # unknown parameters -> "参数未知"
    unreadable: list[str]


CHART_READ_SYSTEM = """你是行情截图的读数器，只做一件事：把图上真正印出来的内容读成结构化数据。

条带：共 {strip_count} 张，从上到下为条带 1..{strip_count}。每条 swing/level 的 strip 必须写它所在的条带序号；只有 1 张时 strip 一律为 1。

硬规则：
- 图上未印出的品种、周期、指标参数一律写 null，并在 unreadable 里各记一条，不得推断。
- price 有任何一位看不清或不确定就写 null，不得按刻度或轴线推算；能看清时保留图上原文末位。
- levels 列出全部可读位，不裁剪；每条带 source（annotation/swing/axis/unknown）与图上依据 basis。
- focus 只给 5-8 个，按固定优先级：图上标注 > 前高/前低 > ONH/ONL > 区间边缘/平台 > 均线。两个数值冲突时保留更极端的那个，另一条写进 unreadable。
- annotations 逐条抄图上文字原文（如 "2 @ 7519.75"），不改写、不合并。
- 非行情截图：market_state 写 unclear，levels 留空，只记录可见文字与版式。
- 你只看到图，没有任何资料证据；不要输出 [n] 引用标记，也不要解释概念。
"""


def _failure_reason(exc: Exception) -> str:
    if isinstance(exc, TimeoutError) or type(exc).__name__ == "APITimeoutError":
        return "timeout"
    if isinstance(exc, (ValidationError, ValueError)):
        return "invalid_structure"
    return "no_content"


def _clamp_strips(read: ChartRead, strip_count: int) -> ChartRead:
    """A strip outside 1..N is a reading error, not a new location.

    Snap to the nearest real strip and say so; the price is left untouched.
    """
    moved = 0
    for item in (*read.swings, *read.levels):
        strip = min(max(item.strip, 1), strip_count)
        if strip != item.strip:
            item.strip = strip
            moved += 1
    if moved:
        read.unreadable.append(f"条带序号越界 {moved} 处，已归到最近的条带")
    return read


def read_chart(
    images: list[bytes], *, client=None
) -> tuple[ChartRead | None, dict[str, str] | None]:
    """One structured read of the uploaded chart strips.

    Returns ``(chart_read, None)`` on success and ``(None, {"reason", "detail"})``
    on failure. Exactly one model attempt: a failed read must not burn the
    120s chart budget on a retry.
    """
    client = client or gateway
    if not images:
        return None, {"reason": "no_content", "detail": "没有可读的图片"}
    try:
        strip_count = sum(_strip_count(data) for data in images)
    except Exception as exc:  # noqa: BLE001 - an unreadable upload is a read failure
        return None, {"reason": "no_content", "detail": f"{type(exc).__name__}: {exc}"}
    messages = [
        {"role": "system", "content": CHART_READ_SYSTEM.format(strip_count=strip_count)},
        {"role": "user", "content": "读取这张行情截图，输出结构化图表数据。"},
    ]
    try:
        out = client.chat_json(
            ChartRead,
            messages,
            images=list(images),
            max_tokens=4096,
            timeout=READ_TIMEOUT_S,
            attempts=1,
        )
        if out is None:
            return None, {"reason": "no_content", "detail": "模型未返回内容"}
    except Exception as exc:  # noqa: BLE001 - a failed read never breaks the run
        detail = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, json.JSONDecodeError):
            detail = f"invalid JSON: {detail}"
        return None, {"reason": _failure_reason(exc), "detail": detail[:300]}
    return _clamp_strips(out, strip_count), None
