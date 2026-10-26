"""Evidence-grounded answer generation with citation binding (plan §7.3)."""
from __future__ import annotations

import re
from typing import Any, Mapping

from pydantic import BaseModel, Field

from paknow.chart import ChartRead, is_non_chart
from paknow.gateway import failure_reason, gateway
from paknow.retrieval.pipeline import citation_binding

_ANSWER_SYSTEM = """你是交易价格行为资料的问答助手。只依据提供的证据回答，逐项绑定证据编号 [n]。
- 先分析后举例：答案主体是"理解与分析"——定义、成立条件、判断逻辑、机制、与相似形态的区别；不要以图例清单作为答案主体。
- 图读辅助（视觉观察）只用于帮助你理解形态/结构，不是事实来源：不得作为依据引用，不得罗列"图X具备该特征"。
- 除非用户明确要求举例（图例），否则不要列举具体图片示例；确需展示图形时，把图放入 figure_ids，不要占用正文。
- 明确区分四类内容：资料原文、当前图中可见事实、基于前两者的解释、工具计算结果。
- 证据不足时明确说出"资料未说明/不可确认"，不得编造页码、图号或结论。
- 中文回答；引用用 [n] 标注，n 对应证据编号。
- cited_indices 返回你在回答中实际引用的证据编号（[n] 里的整数，0-based），不要返回其他数字。
- 保留条件与例外，不做无依据的胜率/预测。
"""

_SYNTHESIS_SYSTEM = """你是交易价格行为资料的推理助手。这是一个机制/框架/原理类问题，要给出"带理解的归纳式回答"，不是逐条复述原文。
要求：
1. 先给出概念骨架：该机制/周期/关系如何运作、有哪些阶段或步骤、阶段之间如何转换（用你自己的话归纳）。
2. 再用证据支撑每个关键点，引用编号 [n]；证据之间可以综合、对比、串联。
3. 图读辅助（视觉观察）只用于帮助理解结构，不作为事实依据，不罗列图例；确需展示图形时放入 figure_ids。
4. 明确区分「资料原文表述」「你的归纳」「当前图可见事实」；资料没说的阶段/顺序/时长要明确标注"未说明"。
5. 中文回答；不编造页码、胜率、具体时点。
cited_indices 返回你实际引用的证据编号（[n] 里的整数，0-based）。
"""


_CHART_PRIORITY = "图上标注 > 前高/前低 > ONH/ONL > 区间边缘/平台 > 均线"

_CHART_SYSTEM = f"""你是交易价格行为资料的图表分析助手。用户上传了截图，可能既有图表也有正文；图上事实由前置读图给出，资料证据只用于解释与核对。

输出三层，顺序严格：
一、图上可读位（完整清单）：把「可读位」区逐条全列，条数必须与给定条数完全一致；每条写出价位、来源、条带、依据；价位为「看不清」的照写「看不清」，不推算、不补全、不合并、不按建议裁剪。用户指定排序（如价格从高到低）就按用户顺序重排，未指定时保持给定顺序。这些行以「图:」开头，不得挂 [n]（ADR-0002）。
二、建议关注位（5–8 个）：从第一层收敛，每个给一句理由，理由体现固定优先级 {_CHART_PRIORITY}。两个价位数值冲突时保留更极端的那个，另一条写进「不确定」，第一层与第二层都不得再把被舍弃的数值当成可读位。均线类指标只说明存在，参数未知时不给具体价位。
三、资料依据 + 归纳：资料如何定义/使用这些结构，引用 [n]；归纳不超过 5 条，区分你的判断与资料原文。只有本层可以出现 [n]。

- 用户要求「只给清单」：只输出第一层，第二层最多一句话，不写第三层。
- 看不清、没标注的数值一律写「看不清」，绝不按轴线推算或补全。
- 图上没有直接印出的信息（品种、周期、指标参数、数据来源、成交量含义）不要推断，统一写「图上未标注」。
- 图上没有的位置不要列，不要为凑数编价位。
- 中文回答；引用用 [n]，n 对应证据编号；cited_indices 只返回实际引用的证据编号。
"""

_CHART_FALLBACK_SYSTEM = """你是交易价格行为资料的图表分析助手。用户上传了截图，可能既有图表也有正文；这次没有结构化读图结果，图上事实由你直接看图得出，资料证据只用于解释与核对。

输出顺序（严格遵守）：
1. 先列出图上真正印出的价位（可读位），一条一行，每条以「图:」开头并写出它在图上的依据（标注原文、箭头/横线位置、哪根K线）；用户指定顺序（如价格从高到低）就按用户顺序，未指定时按图上从上到下。
2. 这些行不可引用（ADR-0002）：不得挂 [n]，不得说成出自资料。
3. 再给「资料依据」：资料如何定义/使用这些结构，引用 [n]。
4. 最后「归纳」：不超过 5 条，区分你的判断与资料原文。

- 看不清、没标注的数值一律写「看不清」，绝不按轴线推算或补全。
- 用户要清单就只给清单，不要改写成概念综述，不要先讲定义。
- 图上没有直接印出的信息（品种、周期、指标参数、数据来源、成交量含义）不要推断，统一写「图上未标注」。
- 图上没有的位置不要列，不要为凑数编价位。
- 如果这张图不是行情图表（纯文字页、产品界面、无关照片），第一句直接说明「这不是行情图表」，只摘图上可见的文字与版式，不列价位、不展开资料依据。
- 中文回答；引用用 [n]，n 对应证据编号；cited_indices 只返回实际引用的证据编号。
"""

_NON_CHART_SYSTEM = """
读图结论：这张图不是行情图表（market_state=unclear，没有可读位）。
- 第一句直接说「这不是行情图表」，不要硬当成行情来分析。
- 上面的三层输出不适用：之后只摘图上可见的文字与版式（标题、按钮、正文片段），说明这些文字写了什么；不推断图上没有印出的结论。
- 不写「关键位」「可读位」清单，不展开资料依据或教科书概念讲解；证据区没有对应内容就不要硬引。
"""


#: Shown when the answer call itself fails to parse: a truncated JSON must leave
#: a run the user can see and retry, not an exception out of ``run_query``.
_DEGRADED_ANSWER = "本次生成失败：模型输出被截断或结构非法，没有可展示的答案。请重试本问题。"


class Answer(BaseModel):
    answer: str
    cited_indices: list[int] = Field(default_factory=list)
    figure_ids: list[str] = Field(default_factory=list)
    uncertainty: list[str] = Field(default_factory=list)


def _render_unit(i: int, u: dict[str, Any]) -> str:
    loc = []
    if u.get("chapter_path"):
        loc.append(" › ".join(u["chapter_path"]))
    if u.get("pdf_page"):
        loc.append(f"PDF第{u['pdf_page']}页")
    if u.get("printed_page"):
        loc.append(f"印刷页{u['printed_page']}")
    if u.get("figure_label"):
        loc.append(str(u["figure_label"]))
    src = " · ".join(loc) or "未知出处"
    tags = []
    if u.get("is_parent"):
        tags.append("父块上下文")
    if u.get("is_adjacent"):
        tags.append("邻接段落")
    if u.get("provenance") == "derived_observation":
        tags.append("视觉观察·非原文")
    tag = f"（{'/'.join(tags)}）" if tags else ""
    return f"[{i}] ({src}){tag} {u['text']}"


_LEVEL_SOURCE = {
    "annotation": "图上标注",
    "swing": "前高/前低",
    "axis": "轴刻度",
    "unknown": "不明",
}
_MARKET_STATE = {
    "trend_up": "上行趋势",
    "trend_down": "下行趋势",
    "trading_range": "区间震荡",
    "unclear": "不明",
}
# read content can never read as a citation (ADR-0002), even when the screenshot
# itself contains a previous answer with [n] markers in it
_CITATION = re.compile(r"\s*\[\d+\]")


def _plain(value: Any) -> str:
    return _CITATION.sub("", str(value))


def _fmt_price(price: float | None) -> str:
    return "看不清" if price is None else f"{price:g}"


def _render_chart_read(read: Mapping[str, Any]) -> str:
    """Deterministic render: layer 1 is pre-numbered so the answer copies it
    instead of re-reading (or trimming) the levels list."""
    def printed(value: Any) -> str:
        return "图上未标注" if value in (None, "") else _plain(value)

    lines = [
        f"品种：{printed(read.get('instrument'))} ｜ 周期：{printed(read.get('timeframe'))}"
        f" ｜ 市场状态：{_MARKET_STATE.get(read.get('market_state'), '不明')}"
        f" ｜ 形态：{'、'.join(_plain(p) for p in read.get('patterns') or []) or '图上未标注'}",
        f"指标：{'、'.join(_plain(i) for i in read.get('indicators') or []) or '图上未标注'}",
        f"图上标注原文：{'；'.join(_plain(a) for a in read.get('annotations') or []) or '图上未标注'}",
    ]
    if read.get("swings"):
        lines.append(
            "摆动高低点："
            + "；".join(
                f"{'高' if s.get('kind') == 'high' else '低'} {_fmt_price(s.get('price'))}"
                f"（条带 {s.get('strip', 1)}，{_plain(s.get('basis')) if s.get('basis') else '图上未标注'}）"
                for s in read["swings"]
            )
        )
    levels = read.get("levels") or []
    if levels:
        lines.append(
            f"可读位（共 {len(levels)} 条，按图上顺序；答案第一层必须逐条全列，不得按建议裁剪）："
        )
        for i, level in enumerate(levels, 1):
            basis = level.get("basis")
            lines.append(
                f"- {i}. {_fmt_price(level.get('price'))}"
                f" ｜ 来源 {_LEVEL_SOURCE.get(level.get('source'), '不明')}"
                f" ｜ 条带 {level.get('strip', 1)}"
                f" ｜ 依据 {_plain(basis) if basis else '图上未标注'}"
            )
    else:
        lines.append("可读位：图上没有可读价位，不要编造价位清单。")
    if read.get("focus"):
        lines.append(f"读图初筛（5–8 个，按固定优先级 {_CHART_PRIORITY}）：")
        lines.extend(
            f"- {_fmt_price(f.get('price'))}：{_plain(f.get('why')) if f.get('why') else '图上标注'}"
            for f in read["focus"]
        )
    if read.get("unreadable"):
        lines.append("不确定项：" + "；".join(_plain(u) for u in read["unreadable"]))
    return "图上读数（结构化，来自图片本身，不是可引用证据）：\n" + "\n".join(lines)


def build_context(
    units: list[dict[str, Any]], index: Mapping[str, int] | None = None
) -> str:
    """Render numbered evidence; ``index`` is the pipeline's [n] binding."""
    return "\n\n".join(
        _render_unit(
            index.get(str(u.get("evidence_id")), i) if index else i, u
        )
        for i, u in enumerate(units)
    )


def graph_locator(entry: dict[str, Any]) -> str:
    loc = []
    if entry.get("chapter_path"):
        loc.append(" › ".join(entry["chapter_path"]))
    if entry.get("pdf_page"):
        loc.append(f"PDF第{entry['pdf_page']}页")
    if entry.get("printed_page"):
        loc.append(f"印刷页{entry['printed_page']}")
    if entry.get("figure_label"):
        loc.append(str(entry["figure_label"]))
    if entry.get("epub_href"):
        loc.append("EPUB " + str(entry["epub_href"]).rsplit("/", 1)[-1])
    return " · ".join(loc) or "未知出处"


# the only mapping point between what extraction stores and what the prompt
# renders: qualifiers carry ``context`` (背景) and ``modality`` (语气)
_QUALIFIER_LABELS = {"context": "背景", "modality": "语气"}


def _claim_conditions(qualifiers: Mapping[str, Any] | None) -> str:
    """Render a claim's stored qualifiers so it never reads as absolute."""
    if not isinstance(qualifiers, Mapping):
        return ""
    parts: list[str] = []
    for key, label in _QUALIFIER_LABELS.items():
        value = qualifiers.get(key)
        if isinstance(value, (list, tuple)):
            value = "、".join(str(item) for item in value if item)
        if value:
            parts.append(f"{label}={value}")
    return "；".join(parts)


def render_graph_context(entries: list[dict[str, Any]]) -> list[str]:
    """One compact non-citable line per graph entry, each carrying its id.

    Entries already present in the citable text keep only a ``已见 [n]``
    pointer so the body is never duplicated and the citation binding stays
    intact. A claim's stored qualifiers are printed inline so a conditioned
    claim does not read as an unconditional rule.
    """
    lines: list[str] = []
    for entry in entries:
        kind = "机制" if entry.get("source") == "mechanism" else "分类"
        claim = entry.get("claim")
        if claim:
            desc = f"{claim.get('subject', '')} → {claim.get('predicate', '')}"
            conditions = _claim_conditions(claim.get("qualifiers"))
            if conditions:
                desc += f"（条件：{conditions}）"
        elif entry.get("relation"):
            desc = f"{entry.get('from_label', '')} -{entry['relation']}-> {entry.get('to_label', '')}"
        else:
            desc = entry.get("from_label") or "关联证据"
        ref = f"证据 {entry.get('evidence_id')}"
        if entry.get("seen_index") is not None:
            ref += f"（已见 [{entry['seen_index']}]）"
            body = ""
        else:
            body = f" | {(entry.get('text') or '')[:220]}"
        lines.append(
            f"- （不可引用·仅理解辅助）[{kind}] {desc} | {ref} | {graph_locator(entry)}{body}"
        )
    return lines


def generate(
    query: str,
    units: list[dict[str, Any]],
    images: list[bytes] | None = None,
    *,
    mode: str = "extractive",
    # reasoning tokens share max_tokens with the content: the chart read needed
    # 8192 for the same reason, and an answer re-sends the strips on top of it
    max_tokens: int = 16384,
    graph_evidence: list[dict[str, Any]] | None = None,
    citation_index: Mapping[str, int] | None = None,
    toc_context: list[dict[str, Any]] | None = None,
    fallback: str | None = None,
    barrefs: list[dict[str, Any]] | None = None,
    wants_examples: bool = False,
    figure_catalog: list[dict[str, Any]] | None = None,
    chart_read: ChartRead | dict[str, Any] | None = None,
) -> dict[str, Any]:
    # derived visual observations are understanding aids, NOT citable evidence:
    # they go into an unnumbered block so citation indices stay consistent
    evidence = [u for u in units if u.get("provenance") != "derived_observation"]
    visual = [u for u in units if u.get("provenance") == "derived_observation"]
    index = dict(citation_index) if citation_index is not None else citation_binding(evidence)
    context = build_context(evidence, index)
    parts = [f"问题：{query}"]
    # chart questions answer from the structured read the graph made upstream;
    # the read is uncitable and carries no [n], so citations still point at
    # evidence. generate never reads the image: the graph's read node owns that.
    if isinstance(chart_read, ChartRead):
        chart_read = chart_read.model_dump()
    if mode == "chart" and chart_read:
        parts.append(_render_chart_read(chart_read))
    if toc_context:
        parts.append(
            "目录框架（相关章节，用于定位知识范围，不直接引用编号）：\n"
            + "\n".join(
                f"- 《{t.get('doc_title', '')}》 {' › '.join(t['chapter_path'])}"
                for t in toc_context
            )
        )
    if graph_evidence:
        parts.append(
            "图谱扩展（不可引用，仅作理解辅助；引用编号只指向「证据」区）：\n"
            + "\n".join(render_graph_context(graph_evidence))
        )
    if visual:
        parts.append(
            "图读辅助（对图中可见事实的视觉观察，仅用于帮助你理解形态与结构，"
            "不是可引用的事实来源，不要引用、不要罗列）：\n"
            + "\n".join(
                f"- 图{u.get('figure_label') or '?'}：{u['text'][:220]}"
                for u in visual[:6]
            )
        )
    if barrefs:
        parts.append(
            "图内K线指代解析（结构化标注，用于定位图中具体K线）：\n"
            + "\n".join(
                f"- {' '.join(b['bars'])} {b['relation']} {b['pattern']}（{b['snippet']}）"
                for b in barrefs
                if b.get("bars")
            )
        )
    if fallback:
        parts.append(f"证据不足时的处理要求：{fallback}")
    if wants_examples:
        parts.append(
            "用户要求举例：可以用图例支撑，通过 figure_ids 返回要展示的图。"
        )
        if figure_catalog:
            parts.append(
                "可选图例（figure_ids 只能从这里选，返回 figure_id 原值；"
                "没有合适的图就返回空数组，禁止编造）：\n"
                + "\n".join(
                    f"- {f['figure_id']}（图{f.get('figure_label') or '?'}）"
                    for f in figure_catalog[:12]
                )
            )
    parts.append(f"证据（引用编号 [n]）：\n{context}")
    user = "\n\n".join(parts)
    # a structured read gets the layered prompt; no read falls back to the
    # single-stage prompt that reads the picture directly
    if mode == "chart":
        system = _CHART_SYSTEM if chart_read else _CHART_FALLBACK_SYSTEM
    else:
        system = _SYNTHESIS_SYSTEM if mode == "synthesis" else _ANSWER_SYSTEM
    # only a non-chart read gets the refusal; a genuine chart keeps the layers
    if mode == "chart" and is_non_chart(chart_read):
        system += _NON_CHART_SYSTEM
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    try:
        ans = gateway.chat_json(Answer, messages, images=images, max_tokens=max_tokens)
    except Exception as exc:  # noqa: BLE001 - one bad answer never aborts the run
        # a truncated JSON (`{"answer":"` and no closing) used to escape
        # run_query and surface as an HTTP 500 with nothing to show
        return {
            "answer": _DEGRADED_ANSWER,
            "evidence_ids": [],
            "figure_ids": [],
            "uncertainty": [],
            "chart_read": chart_read,
            "chart_read_status": "structured" if chart_read else "unstructured",
            # machine-readable marker: a reader can tell this placeholder from
            # a genuinely short answer without guessing by length
            "degraded": True,
            "degradation": {
                "reason": failure_reason(exc),
                "detail": f"{type(exc).__name__}: {exc}"[:300],
            },
        }
    by_number = {
        index.get(str(unit.get("evidence_id")), position): unit
        for position, unit in enumerate(evidence)
    }
    evidence_ids = [
        by_number[i]["evidence_id"]
        for i in ans.cited_indices
        if i in by_number and by_number[i].get("evidence_id")
    ]
    return {
        "answer": ans.answer,
        "evidence_ids": evidence_ids,
        "figure_ids": ans.figure_ids,
        "uncertainty": ans.uncertainty,
        "chart_read": chart_read,
        "chart_read_status": "structured" if chart_read else "unstructured",
    }
