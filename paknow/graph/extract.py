"""Claim/concept extraction into the evidence graph (plan §6.2, §6.3).

Extraction is a *candidate* step; extracted claims are ``pending`` review and
never treated as authoritative source. Runs on a bounded high-value subset.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from paknow.db import SessionLocal
from paknow.db.models import ClaimRow, GraphEdgeRow, GraphNodeRow
from paknow.gateway import gateway
from paknow.ids import stable_id

_EXTRACT_SYSTEM = """你是价格行为资料的领域抽取器。从给定文本提取：
1. concepts: 出现的规范概念（高二/High 2、突破回调、失败突破、双底多头旗形等，用规范名）。
2. claims: 带条件的主张。每条含 subject(形态/概念)、predicate(断言)、polarity(positive/negative/neutral)、context(成立背景)、modality(语气: may/is/never)。
只抽取文本中明确陈述的内容；不要凭空补造条件或胜率。没有明确主张就返回空列表。"""


class ExtractedClaim(BaseModel):
    subject: str
    predicate: str
    polarity: str = "positive"
    context: list[str] = Field(default_factory=list)
    modality: str = "may"


class ExtractionResult(BaseModel):
    concepts: list[str] = Field(default_factory=list)
    claims: list[ExtractedClaim] = Field(default_factory=list)


def extract_from_text(text: str) -> ExtractionResult:
    messages = [
        {"role": "system", "content": _EXTRACT_SYSTEM},
        {"role": "user", "content": text},
    ]
    try:
        return gateway.chat_json(ExtractionResult, messages, max_tokens=1024)
    except Exception:
        return ExtractionResult()


def persist_extraction(doc_id: str, evidence_id: str, result: ExtractionResult) -> None:
    with SessionLocal() as s:
        for concept in result.concepts:
            node_id = stable_id("node", concept)
            if s.get(GraphNodeRow, node_id) is None:
                s.add(GraphNodeRow(node_id=node_id, node_type="concept", label=concept, concept_id=concept))
        for c in result.claims:
            claim_id = stable_id("claim", evidence_id, c.subject, c.predicate)
            if s.get(ClaimRow, claim_id) is None:
                s.add(
                    ClaimRow(
                        claim_id=claim_id,
                        subject=c.subject,
                        predicate=c.predicate,
                        polarity=c.polarity,
                        qualifiers={"context": c.context, "modality": c.modality},
                        supporting_evidence=[evidence_id],
                        extraction_status="pending",
                    )
                )
                # link claim -> source evidence unit via SUPPORTED_BY
                s.add(
                    GraphEdgeRow(
                        edge_id=stable_id("edge", claim_id, evidence_id),
                        from_node=claim_id,
                        to_node=evidence_id,
                        relation="SUPPORTED_BY",
                        source=evidence_id,
                    )
                )
        s.commit()
