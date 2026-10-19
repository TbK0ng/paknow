"""Evidence sufficiency + bounded re-retrieval planning (plan §4.2, §7.1)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from paknow.gateway import gateway


class Sufficiency(BaseModel):
    sufficient: bool
    missing: list[str] = Field(default_factory=list)
    conflicting: list[str] = Field(default_factory=list)
    next_query: str | None = None
    reason: str = ""


_SUFFICIENCY_SYSTEM = """你是证据核验器。给定问题、所需证据类型、已检索到的证据摘要，判断证据是否充分。
输出：sufficient(是否足够)、missing(缺失的条件/证据)、conflicting(互相冲突的来源)、next_query(若有明确缺口且值得补检索，给出一条新的检索问句，否则 null)、reason(一句话理由)。
只有明确缺口才给 next_query；不要为了"看起来努力"而改写问题。"""


def check_sufficiency(
    query: str,
    evidence_needs: list[str],
    hits: list[dict[str, Any]],
    *,
    round_: int,
    max_rounds: int,
) -> Sufficiency:
    if not hits:
        return Sufficiency(
            sufficient=False,
            missing=["无检索结果"],
            next_query=query if round_ < max_rounds else None,
            reason="零命中",
        )
    if round_ >= max_rounds:
        return Sufficiency(sufficient=True, reason="达到检索轮次上限，按现有证据回答")

    summaries = [f"[{i}] {h['text'][:120]}" for i, h in enumerate(hits[:15])]
    user = (
        f"问题：{query}\n所需证据：{evidence_needs}\n\n已检索证据摘要：\n"
        + "\n".join(summaries)
    )
    messages = [
        {"role": "system", "content": _SUFFICIENCY_SYSTEM},
        {"role": "user", "content": user},
    ]
    try:
        return gateway.chat_json(Sufficiency, messages, max_tokens=512)
    except Exception:
        # fail open: answer with what we have rather than loop forever
        return Sufficiency(sufficient=True, reason="核验器失败，按现有证据回答")
