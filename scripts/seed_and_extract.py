"""Seed the trend-lifecycle concept into the graph, then re-extract claims from
the newly added books (pabook.pdf + Price Action 笔记.pdf) so they join the graph.

Every writer passes its :class:`~paknow.graph.contracts.EdgeOrigin`, so a re-run
is idempotent and never relabels edges written by another producer.
"""
from __future__ import annotations

import time

from sqlalchemy import func

from paknow.db import SessionLocal, init_db
from paknow.db.models import (
    ClaimRow,
    DocumentRow,
    EvidenceUnitRow,
    GraphEdgeRow,
    GraphNodeRow,
)
from paknow.graph.contracts import EdgeOrigin
from paknow.graph.extract import extract_from_text, persist_extraction
from paknow.ids import stable_id

# --------------------------------------------------------------------------- #
# 1. seed the market-cycle / trend-lifecycle concept + 4 stages + transitions
# --------------------------------------------------------------------------- #
STAGES = ["spike", "tight_channel", "broad_channel", "trading_range"]
LABELS = {
    "spike": "脉冲 Spike",
    "tight_channel": "紧密通道 Tight Channel",
    "broad_channel": "宽松通道 Broad Channel",
    "trading_range": "交易区间 Trading Range",
}


def seed_trend_lifecycle(session_factory=SessionLocal) -> None:
    with session_factory() as s:
        lifecycle = stable_id("node", "trend_lifecycle")
        s.merge(
            GraphNodeRow(
                node_id=lifecycle,
                node_type="concept",
                label="趋势生命周期 / 市场周期",
                concept_id="trend_lifecycle",
                meta={
                    "stages": STAGES,
                    "order": "spike→tight_channel→broad_channel→trading_range→(新突破)→spike",
                    "note": "多数趋势按此顺序演变，非所有趋势都走完四阶段",
                },
            )
        )
        stage_ids: dict[str, str] = {}
        for st in STAGES:
            nid = stable_id("node", st)
            s.merge(GraphNodeRow(node_id=nid, node_type="concept", label=LABELS[st], concept_id=st))
            stage_ids[st] = nid
            s.merge(
                GraphEdgeRow(
                    edge_id=stable_id("edge", nid, lifecycle),
                    from_node=nid,
                    to_node=lifecycle,
                    relation="PART_OF",
                    source="seed",
                    origin=EdgeOrigin.seed.value,
                )
            )
        for i in range(len(STAGES)):
            a, b = STAGES[i], STAGES[(i + 1) % len(STAGES)]
            s.merge(
                GraphEdgeRow(
                    edge_id=stable_id("edge", stage_ids[a], stage_ids[b], "trans"),
                    from_node=stage_ids[a],
                    to_node=stage_ids[b],
                    relation="TRANSITIONS_TO",
                    source="seed",
                    origin=EdgeOrigin.seed.value,
                )
            )
        s.commit()
    print("seeded trend_lifecycle concept + 4 stages + 4 transition edges", flush=True)


# --------------------------------------------------------------------------- #
# 2. link the cycle claim to its defining evidence in pabook.pdf
# --------------------------------------------------------------------------- #
def link_cycle_claim(session_factory=SessionLocal) -> None:
    with session_factory() as s:
        pabook = s.query(DocumentRow).filter(DocumentRow.source_path.like("%pabook%")).first()
        if pabook is not None:
            units = (
                s.query(EvidenceUnitRow)
                .filter(
                    EvidenceUnitRow.doc_id == pabook.doc_id,
                    EvidenceUnitRow.text.like("%四个阶段%"),
                )
                .all()
            )
            def_unit = next(
                (u for u in units if "一句话定义" in u.text or "依次经历" in u.text),
                units[0] if units else None,
            )
            if def_unit is not None:
                cid = stable_id("claim", "trend_lifecycle_stages", def_unit.evidence_id)
                s.merge(
                    ClaimRow(
                        claim_id=cid,
                        subject="trend_lifecycle",
                        predicate="趋势依次经历四个阶段：脉冲→紧密通道→宽松通道→交易区间",
                        polarity="positive",
                        qualifiers={"context": ["趋势"], "modality": "usually"},
                        supporting_evidence=[def_unit.evidence_id],
                        extraction_status="reviewed",
                    )
                )
                s.merge(
                    GraphEdgeRow(
                        edge_id=stable_id("edge", cid, def_unit.evidence_id),
                        from_node=cid,
                        to_node=def_unit.evidence_id,
                        relation="SUPPORTED_BY",
                        source=def_unit.evidence_id,
                        origin=EdgeOrigin.claim_extract.value,
                    )
                )
                s.commit()
                print(f"linked cycle claim to evidence {def_unit.evidence_id[:12]}", flush=True)


# --------------------------------------------------------------------------- #
# 3. re-extract from the new books' high-value units
# --------------------------------------------------------------------------- #
def reextract_new_books(session_factory=SessionLocal) -> None:
    with session_factory() as s:
        new_docs = [
            d.doc_id
            for d in s.query(DocumentRow)
            .filter(
                DocumentRow.source_path.like("%pabook%")
                | DocumentRow.source_path.like("%Price Action 笔记%")
            )
            .all()
        ]
        units = (
            s.query(EvidenceUnitRow)
            .filter(
                EvidenceUnitRow.doc_id.in_(new_docs),
                (
                    EvidenceUnitRow.block_type.in_(["caption", "heading"])
                    | (func.json_array_length(EvidenceUnitRow.figure_ids) > 0)
                    | EvidenceUnitRow.text.like("%一句话定义%")
                ),
            )
            .all()
        )

    seen: set[str] = set()
    uniq = [u for u in units if not (u.evidence_id in seen or seen.add(u.evidence_id))]
    print(f"high-value units (new books): {len(uniq)}", flush=True)

    n_concepts = n_claims = 0
    t0 = time.time()
    for i, u in enumerate(uniq):
        r = extract_from_text(u.text)
        persist_extraction(u.doc_id, u.evidence_id, r)
        n_concepts += len(r.concepts)
        n_claims += len(r.claims)
        if (i + 1) % 50 == 0:
            print(
                f"  {i+1}/{len(uniq)} concepts={n_concepts} claims={n_claims} "
                f"({time.time()-t0:.0f}s)",
                flush=True,
            )
    print(
        f"DONE new-book extraction: concepts={n_concepts} claims={n_claims} "
        f"in {time.time()-t0:.0f}s",
        flush=True,
    )


def main() -> None:
    init_db()
    seed_trend_lifecycle()
    link_cycle_claim()
    reextract_new_books()


if __name__ == "__main__":
    main()
