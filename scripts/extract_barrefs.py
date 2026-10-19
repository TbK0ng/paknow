"""Extract structured bar-number references from figure-linked units (plan §6.1).

Candidates: caption units + units carrying figure_ids whose text contains a
bar-number pattern. Batched LLM extraction, idempotent by ref_id.
"""
from __future__ import annotations

import hashlib
import re
import time

from pydantic import BaseModel, Field
from sqlalchemy import func, or_

from paknow.config import settings
from paknow.db import SessionLocal, init_db
from paknow.db.models import BarRefRow, EvidenceUnitRow
from paknow.gateway import gateway

_BAR_HINT_RE = re.compile(
    r"(?:K\s*线|bar\w*|B)\s*[0-9０-９]|\d+\s*(?:号|根|个)\s*K|第\s*[0-9０-９]+\s*[Kk]",
    re.I,
)
_BATCH = 6
_MAX_TOKENS = 2400


class BarRef(BaseModel):
    idx: int = 0  # 0-based fragment index within the batch
    bars: list[str] = Field(default_factory=list)
    pattern: str = ""
    relation: str = ""
    snippet: str = ""


class BarRefs(BaseModel):
    refs: list[BarRef] = Field(default_factory=list)


_SYSTEM = """你是图表K线指代解析器。给定若干图注或图表说明段落（编号前缀 [n]），提取"带明确编号的K线指代"：
- idx: 该 ref 所属片段的编号（[n] 里的整数，0-based）。
- bars: 涉及的K线编号，统一写 "K线N"（如 "K线2"、"K线3"）；区间写 "K线2-4"。
- pattern: 这些K线构成的形态/事件中文名（如 双底、突破、回调、牛旗、交易区间），没有就留空。
- relation: 关系动词（构成/测试/突破/跌破/回撤/双顶/第二次测试…），没有就留空。
- snippet: 原文关键片段（≤50字）。
叙述性说明段落（如"图7.4从K线2急跌至K线3……"）也要提取——只要出现带编号的K线指代就提取，不管片段是图注还是说明。没有编号指代才跳过。每个片段可提取 0~4 条。"""


def _candidates() -> list[EvidenceUnitRow]:
    with SessionLocal() as s:
        rows = (
            s.query(EvidenceUnitRow)
            .filter(
                or_(
                    EvidenceUnitRow.block_type == "caption",
                    func.json_array_length(EvidenceUnitRow.figure_ids) > 0,
                )
            )
            .all()
        )
    return [r for r in rows if _BAR_HINT_RE.search(r.text or "")]


def extract(only_missing: bool = False) -> int:
    settings.ensure_dirs()
    init_db()
    units = _candidates()
    if only_missing:
        with SessionLocal() as s:
            covered = {
                r[0]
                for r in s.query(BarRefRow.evidence_id)
                .filter(BarRefRow.evidence_id.isnot(None))
                .distinct()
                .all()
            }
        units = [u for u in units if u.evidence_id not in covered]
    print(f"candidates: {len(units)} (only_missing={only_missing})", flush=True)
    saved = 0
    t0 = time.time()
    for start in range(0, len(units), _BATCH):
        batch = units[start : start + _BATCH]
        if not batch:
            continue
        user_lines = [f"[{i}] {u.text[:400]}" for i, u in enumerate(batch)]
        result = None
        for _attempt in range(2):
            try:
                result = gateway.chat_json(
                    BarRefs,
                    [
                        {"role": "system", "content": _SYSTEM},
                        {"role": "user", "content": "片段：\n" + "\n".join(user_lines)},
                    ],
                    max_tokens=_MAX_TOKENS,
                )
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
        if result is None:
            print(f"batch {start} failed: {last_err}", flush=True)
            continue
        for ref in result.refs:
            if not (0 <= ref.idx < len(batch)):
                continue
            if not ref.bars:
                continue
            u = batch[ref.idx]
            bars_key = "|".join(sorted(b.strip() for b in ref.bars))
            ref_id = hashlib.sha1(
                f"{u.doc_id}:{u.evidence_id}:{bars_key}:{ref.pattern}".encode()
            ).hexdigest()[:16]
            fig_id = (u.figure_ids or [None])[0]
            with SessionLocal() as s:
                s.merge(
                    BarRefRow(
                        ref_id=ref_id,
                        doc_id=u.doc_id,
                        figure_id=fig_id,
                        evidence_id=u.evidence_id,
                        pattern=ref.pattern or "",
                        bars=ref.bars,
                        relation=ref.relation or "",
                        snippet=ref.snippet or "",
                    )
                )
                s.commit()
            saved += 1
        if start // _BATCH % 25 == 0:
            el = time.time() - t0
            print(f"{start}/{len(units)} saved={saved} ({el:.0f}s)", flush=True)
    print(f"done: saved {saved} barrefs in {time.time()-t0:.0f}s", flush=True)
    return saved


if __name__ == "__main__":
    import sys

    extract(only_missing="--only-missing" in sys.argv)
