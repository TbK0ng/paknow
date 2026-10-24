"""意图路由 + 查询构建/改写 (plan §4.1 + all-in-RAG ch4 查询构建/改写).

单次结构化调用同时产出：意图/框架判定、退步抽象(Step-Back)、多查询分解
(Multi-query)、假设性答案(HyDE)、证据需求与路由。检索层用这些扩展后的查询
多路召回并融合，框架类问题走 synthesis 生成。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from paknow.gateway import gateway
from paknow.retrieval.dictionary import expand_query

# A route is a recall channel with its own score. Graph expansion is not a
# route: it runs after recall and never participates in scoring (ADR-0003).
ROUTE_VOCAB = ("hybrid_text", "toc", "visual_page", "numeric")

_QUERY_SYSTEM = """你是交易资料系统的查询理解与构建器。输入用户问题、已识别术语、附件。输出一个结构化查询计划，字段语义：

- intent: knowledge_qa(具体知识/定义) | synthesis(机制/框架/原理/综述) | chart_analysis(上传图表) | backtest(回测/统计) | study(学习路径/全书)。
- is_framework: 问题是否在问"机制/原理/如何运作/如何转换/生命周期/什么关系/框架综述"。这类问题需要退步抽象 + 归纳式回答，而非逐条复述。
- step_back: 把问题抽象成底层概念/更一般的问题（用于检索更上位的内容）。例："市场周期如何转换" → "趋势生命周期：趋势如何从脉冲/通道演变到交易区间，再回到趋势"。不要复述原问。
- sub_queries: 2~4 个检索子问题，覆盖不同角度，中英术语都写。
- hyde: 写一段假设性的完整回答（150~300字，用于召回，不是最终答案），把该问题可能的标准答案骨架写出来。
- evidence_needs: definition/context/figure/counterexample/comparison/toc/cycle(周期阶段)。
- routes: hybrid_text/toc/visual_page/numeric（只列有独立打分的召回通道）。
- wants_examples: 用户是否明确要求举例/图例（"举个例子/有哪些图/哪里出现过/给我看图"）。仅在明确要例子时为 true；理解类问题（"什么是/如何判断/为什么"）为 false。
- metadata: 结构化过滤提示（如 {doc_hint}），没有就 {}。
- data_state: market_ready|screenshot_only|none；请求数值但无行情数据时不得走 numeric。
- 只按问题本身判断，不依赖尚未生成的答案。
"""


class QueryPlan(BaseModel):
    intent: str = "knowledge_qa"
    is_framework: bool = False
    step_back: str = ""
    sub_queries: list[str] = Field(default_factory=list)
    hyde: str = ""
    evidence_needs: list[str] = Field(default_factory=list)
    routes: list[str] = Field(default_factory=list)
    wants_examples: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    data_state: str = "none"
    missing_fields: list[str] = Field(default_factory=list)
    max_search_rounds: int = 3


def route(query: str, images: list[bytes] | None = None) -> QueryPlan:
    terms = expand_query(query)
    user = (
        f"问题：{query}\n\n"
        f"已识别术语（canonical/中文/英文）：{terms}\n\n"
        f"附件数量：{len(images or [])}"
    )
    messages = [
        {"role": "system", "content": _QUERY_SYSTEM},
        {"role": "user", "content": user},
    ]
    try:
        # reasoning tokens count against the budget; images make the model think
        # far longer, and a truncated reply degrades the whole run to a text answer
        plan = gateway.chat_json(
            QueryPlan, messages, images=images, max_tokens=4096 if images else 2048
        )
        # drop anything outside the scored-channel vocabulary, including the
        # retired graph_local label; never leave the retrieval seam route-less
        plan.routes = [r for r in plan.routes if r in ROUTE_VOCAB]
        if not plan.routes:
            plan.routes = ["hybrid_text"]
        return plan
    except Exception:
        # fail-safe: degrade to a plain plan, but never forget that the user
        # attached a picture — an image question answered as knowledge_qa is
        # exactly the "concept survey instead of a reading" failure
        if images:
            return QueryPlan(
                intent="chart_analysis",
                routes=["visual_page", "hybrid_text"],
                evidence_needs=["figure", "context"],
            )
        return QueryPlan(
            intent="knowledge_qa",
            is_framework=False,
            sub_queries=[],
            evidence_needs=["definition", "context"],
            routes=["hybrid_text"],
        )


def retrieval_queries(plan: QueryPlan, query: str) -> list[str]:
    """Assemble the expanded query set for retrieval (original + step_back +
    sub_queries + hyde + disambiguation variants), deduped and bounded."""
    out: list[str] = []
    seen: set[str] = set()
    for q in [query, plan.step_back, *plan.sub_queries, plan.hyde]:
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    # ambiguous abbreviations (BTC/AI/...) get a context-biased variant
    for ctx in expand_query(query).get("disambig", []):
        q = f"{query} {ctx}".strip()
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out[:6]
