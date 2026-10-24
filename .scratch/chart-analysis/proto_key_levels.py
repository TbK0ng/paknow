"""Throwaway probe: key levels instead of OCR.

Answers one question — does a prompt that treats printed numbers as *aids* and
estimates structural prices from the axis (half a grid interval) produce
levels worth calling key? Not wired into the product: it only swaps the read
stage's system prompt and reuses ``read_chart`` unchanged (same schema, same
one attempt, same 120s budget).

Output: ``.scratch/chart-analysis/proto/<id>.json``.
"""
from __future__ import annotations

import json
import pathlib
import sys

import paknow.chart as chart
from paknow.gateway import gateway, strip_count

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = ROOT / ".scratch" / "chart-analysis" / "proto"

PROMPT = """你是行情截图的读数器。任务不是抄图上所有数字，而是找出**关键位**并给出价位。

关键位，按优先级：
1. 图上标注（数字编号、箭头、"2 @ 7519.75" 这类文字）所指的**位置**；
2. 前高 / 前低；
3. ONH / ONL（隔夜/常态高点低点）；
4. 区间边缘、平台边缘；
5. 摆动高低点、信号棒高低点。

关于数字的口径（重要）：
- 图上标注里的数字只是**辅助定位**，未必是关键价；关键价以该位置在价格轴上的读数为准。
- 多数关键位图上**没有印出价位**。这时必须按轴刻度插值估算：先读该位置上下相邻的两条刻度，
  再给出估算值，精度为**半格**（相邻两条刻度间距的一半）。
- 每条 level 的 basis 必须同时写清：所在条带、位置依据（例如"标注 2 所指的转折高点"）、
  插值依据（例如"介于 187.00 与 186.00 之间、靠上四分之一"）。
- 表头数值（L=、MAX 20 (20)、涨跌幅、成交量）与价格轴刻度本身**都不是**关键位，不得列入 levels。

输出要求：
- levels：只列关键位，5-8 条；不裁剪真实结构，不为凑数编价位。
- swings：每个可见的摆动高低点一条，price 用同样的插值口径估算；确实无法估算才写 null 并在 unreadable 说明。
- focus：从 levels 里挑 5-8 条给一句筛选理由，理由体现上面的优先级。
- 每张图共 {strip_count} 条带；strip 写该位置所在的条带序号。
- 只输出 JSON；不要写 [n] 引用，不要解释概念。

语言：除 annotations 里照抄图上的英文原文之外，其余所有字段（instrument、timeframe、basis、why、
patterns、indicators、unreadable）一律用**中文**写。图上只印英文术语时写成「中文（English）」。

形态 patterns：每条都必须写清四件事，用这个格式：
  名称：<标准中文名>；位置：<条带 + 标注号或时间段>；依据：<哪几根K线、哪条趋势线/水平线>；把握：高/中/低
- 名称优先取自：交易区间、上升趋势、下降趋势、交易区间中的趋势、尖峰、通道、楔形（三推）、
  双顶、双底、头肩顶、头肩底、旗形、三角形、扩张三角形、内包K线、外包K线、假突破、突破回调、
  测量移动、高低1/2/3/4。都不是就写「其他：<一句话描述>」。
- 只写名称不算识别：没有位置和依据的条目不输出；图上确实没有形态就返回空数组。
"""


INFO_PROMPT = "读取这张行情截图，输出结构化图表数据。"


def read_once(data: bytes, strips: int) -> tuple[object | None, str | None]:
    """One structured call at a budget the estimation prompt actually needs.

    ``read_chart`` caps at 8192; with estimation the reasoning burns past that,
    so the probe asks for 16384 and records the failure instead of degrading.
    """
    messages = [
        {"role": "system", "content": PROMPT.format(strip_count=strips)},
        {"role": "user", "content": INFO_PROMPT},
    ]
    try:
        out = gateway.chat_json(
            chart.ChartRead,
            messages,
            images=[data],
            max_tokens=16384,
            timeout=180,
            attempts=1,
        )
    except Exception as exc:  # noqa: BLE001 - probe records, never raises
        return None, f"{type(exc).__name__}: {exc}"[:300]
    return out, None


def main() -> int:
    gold = json.loads((ROOT / "evals/gold/chart_v1.json").read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    wanted = set(sys.argv[1:])
    for item in gold["items"]:
        if wanted and item["id"] not in wanted:
            continue
        path = ROOT / item["image"]
        data = path.read_bytes()
        strips = strip_count(data)
        print(f"[{item['id']}] {path.name} strips={strips}", flush=True)
        read, failure = read_once(data, strips)
        if read is None:
            print(f"  FAILED {failure}", flush=True)
            (OUT / f"{item['id']}.json").write_text(
                json.dumps({"id": item["id"], "failure": failure}, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            continue
        read = chart._clamp_strips(read, strips)
        (OUT / f"{item['id']}.json").write_text(
            json.dumps({"id": item["id"], "read": read.model_dump()}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        print(
            f"  levels={len(read.levels)} swings={len(read.swings)} focus={len(read.focus)}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
