"""Evidence-grounded answer generation with citation binding (plan §7.3)."""
from __future__ import annotations

from typing import Any, Mapping

from pydantic import BaseModel, Field

from paknow.gateway import gateway
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


_CHART_SYSTEM = """你是交易价格行为资料的图表分析助手。用户上传了截图，可能既有图表也有正文；图上事实由你直接读，资料证据只用于解释与核对。

输出顺序（严格遵守）：
1. 先直接回答用户问的内容，以读图开头：周期、走势结构、摆动高低点、图上标注的价位与形态，逐条列出，每条以「图:」开头。
2. 读图结果不是可引用证据（ADR-0002）：不得挂 [n]，不得说成出自资料。看不清、没标注的数值一律写「看不清」，绝不按轴线推算或补全。
3. 再给「资料依据」：资料如何定义/使用这些结构，引用 [n]。
4. 最后「归纳」：不超过 5 条，区分你的判断与资料原文。

- 用户要清单就只给清单，按用户指定顺序（如价格从高到低）；不要改写成概念综述，不要先讲定义。
- 图上没有直接印出的信息（品种、周期、指标参数、数据来源、成交量含义）不要推断，统一写「图上未标注」。
- 图上没有的位置不要列，不要为凑数编价位。
- 中文回答；引用用 [n]，n 对应证据编号；cited_indices 只返回实际引用的证据编号。
"""


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


def _graph_locator(entry: dict[str, Any]) -> str:
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


def _claim_conditions(claim: dict[str, Any]) -> str:
    """Compact condition/qualifier text so a conditioned claim never reads absolute."""
    parts: list[str] = []
    for key in ("conditions", "qualifiers", "exceptions"):
        value = claim.get(key)
        if not value:
            continue
        if isinstance(value, dict):
            for name, item in value.items():
                if isinstance(item, (list, tuple)):
                    item = "、".join(str(x) for x in item)
                if item:
                    parts.append(f"{name}={item}")
        elif isinstance(value, (list, tuple)):
            parts.extend(str(item) for item in value if item)
        else:
            parts.append(str(value))
    return "；".join(parts)


def render_graph_context(entries: list[dict[str, Any]]) -> list[str]:
    """One compact non-citable line per graph entry, each carrying its id.

    Entries already present in the citable text keep only a ``已见 [n]``
    pointer so the body is never duplicated and the citation binding stays
    intact. Claim conditions/qualifiers are printed inline so a conditioned
    claim does not read as an unconditional rule.
    """
    lines: list[str] = []
    for entry in entries:
        kind = "机制" if entry.get("source") == "mechanism" else "分类"
        claim = entry.get("claim")
        if claim:
            desc = f"{claim.get('subject', '')} → {claim.get('predicate', '')}"
            conditions = _claim_conditions(claim)
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
            f"- （不可引用·仅理解辅助）[{kind}] {desc} | {ref} | {_graph_locator(entry)}{body}"
        )
    return lines


def generate(
    query: str,
    units: list[dict[str, Any]],
    images: list[bytes] | None = None,
    *,
    mode: str = "extractive",
    max_tokens: int = 8192,
    graph_evidence: list[dict[str, Any]] | None = None,
    citation_index: Mapping[str, int] | None = None,
    toc_context: list[dict[str, Any]] | None = None,
    fallback: str | None = None,
    barrefs: list[dict[str, Any]] | None = None,
    wants_examples: bool = False,
    figure_catalog: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    # derived visual observations are understanding aids, NOT citable evidence:
    # they go into an unnumbered block so citation indices stay consistent
    evidence = [u for u in units if u.get("provenance") != "derived_observation"]
    visual = [u for u in units if u.get("provenance") == "derived_observation"]
    index = dict(citation_index) if citation_index is not None else citation_binding(evidence)
    context = build_context(evidence, index)
    parts = [f"问题：{query}"]
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
    system = {"chart": _CHART_SYSTEM, "synthesis": _SYNTHESIS_SYSTEM}.get(
        mode, _ANSWER_SYSTEM
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    ans = gateway.chat_json(Answer, messages, images=images, max_tokens=max_tokens)
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
    }
