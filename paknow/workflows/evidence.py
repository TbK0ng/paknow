"""Evidence sufficiency + bounded re-retrieval planning (plan §4.2, §7.1)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from paknow.gateway import gateway
from paknow.workflows.generate import graph_locator

# the judge reads a bounded but real view: wider than the 14-item diversity
# window the answer uses, with id + locator so a gap can name what is missing
_JUDGE_ITEMS = 20
_JUDGE_TEXT_CHARS = 400


class Sufficiency(BaseModel):
    sufficient: bool
    missing: list[str] = Field(default_factory=list)
    conflicting: list[str] = Field(default_factory=list)
    next_query: str | None = None
    reason: str = ""


_SUFFICIENCY_SYSTEM = """你是证据核验器。给定问题、所需证据类型、已检索到的证据（每条带证据 id 与出处），判断证据是否充分。
输出：sufficient(是否足够)、missing(缺失的条件/证据)、conflicting(互相冲突的来源)、next_query(若有明确缺口且值得补检索，给出一条新的检索问句，否则 null)、reason(一句话理由)。
只有明确缺口才给 next_query；不要为了"看起来努力"而改写问题。"""


def _evidence_line(i: int, hit: dict[str, Any]) -> str:
    # one line per unit: the item count in the prompt must equal what is judged
    text = " ".join((hit.get("text") or "").split())[:_JUDGE_TEXT_CHARS]
    return f"[{i}] 证据 {hit.get('evidence_id')} | {graph_locator(hit)} | {text}"


def check_sufficiency(
    query: str,
    evidence_needs: list[str],
    hits: list[dict[str, Any]],
    *,
    round_: int,
    max_rounds: int,
) -> tuple[Sufficiency, dict[str, int] | None]:
    """Judge the evidence; the second value audits the view actually sent.

    ``None`` means no prompt went out (zero hits or the round cap), so the run
    trace keeps the scale of the last view the judge really read.
    """
    if not hits:
        return (
            Sufficiency(
                sufficient=False,
                missing=["无检索结果"],
                next_query=query if round_ < max_rounds else None,
                reason="零命中",
            ),
            None,
        )
    if round_ >= max_rounds:
        return Sufficiency(sufficient=True, reason="达到检索轮次上限，按现有证据回答"), None

    lines = [_evidence_line(i, hit) for i, hit in enumerate(hits[:_JUDGE_ITEMS], 1)]
    block = "\n".join(lines)
    user = (
        f"问题：{query}\n所需证据：{evidence_needs}\n\n"
        f"已检索证据（共 {len(lines)} 条，带证据 id 与出处）：\n{block}"
    )
    sent = {"round": round_, "evidence_items": len(lines), "evidence_chars": len(block)}
    messages = [
        {"role": "system", "content": _SUFFICIENCY_SYSTEM},
        {"role": "user", "content": user},
    ]
    try:
        return gateway.chat_json(Sufficiency, messages, max_tokens=512), sent
    except Exception:
        # fail open: answer with what we have rather than loop forever
        return Sufficiency(sufficient=True, reason="核验器失败，按现有证据回答"), sent
