"""Every edge producer records its own ``EdgeOrigin``; re-running a producer
never relabels edges written by another one.
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from paknow.db import Base
from paknow.db.models import (
    ClaimRow,
    DocumentRow,
    EvidenceUnitRow,
    FigureAssetRow,
    GraphEdgeRow,
)
from paknow.graph import extract
from paknow.graph.contracts import EdgeOrigin
from scripts import link_figures_to_graph, seed_and_extract


def _session(monkeypatch=None):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed_doc(Session, *, source_path: str = "book.pdf", doc_id: str = "d1") -> None:
    with Session() as s:
        s.add(
            DocumentRow(
                doc_id=doc_id,
                content_hash=doc_id,
                format="pdf",
                source_path=source_path,
            )
        )
        s.commit()


def _origins(Session) -> dict[str, int]:
    with Session() as s:
        out: dict[str, int] = {}
        for edge in s.query(GraphEdgeRow).all():
            out[edge.origin] = out.get(edge.origin, 0) + 1
        return out


def test_seed_script_writes_seed_origin_and_rerun_is_idempotent():
    Session = _session()
    _seed_doc(Session)

    seed_and_extract.seed_trend_lifecycle(Session)
    first = _origins(Session)
    seed_and_extract.seed_trend_lifecycle(Session)

    assert first == {EdgeOrigin.seed.value: 8}
    assert _origins(Session) == {EdgeOrigin.seed.value: 8}


def test_seed_rerun_never_relabels_claim_edges():
    Session = _session()
    _seed_doc(Session, source_path="pabook.pdf")
    with Session() as s:
        s.add(
            EvidenceUnitRow(
                evidence_id="e1",
                doc_id="d1",
                locator={"doc_id": "d1", "format": "pdf"},
                text="趋势依次经历四个阶段，这是一句话定义",
                text_hash="e1",
                block_type="paragraph",
            )
        )
        s.add(
            GraphEdgeRow(
                edge_id="claim-edge",
                from_node="c1",
                to_node="e1",
                relation="SUPPORTED_BY",
                source="e1",
                origin=EdgeOrigin.claim_extract.value,
            )
        )
        s.commit()

    seed_and_extract.link_cycle_claim(Session)
    seed_and_extract.seed_trend_lifecycle(Session)
    seed_and_extract.link_cycle_claim(Session)

    origins = _origins(Session)
    assert origins[EdgeOrigin.seed.value] == 8
    assert origins[EdgeOrigin.claim_extract.value] == 2  # cycle link + pre-existing
    with Session() as s:
        claim_edges = (
            s.query(GraphEdgeRow).filter(GraphEdgeRow.relation == "SUPPORTED_BY").all()
        )
        assert len(claim_edges) == 2
        assert all(e.origin == EdgeOrigin.claim_extract.value for e in claim_edges)


def test_figure_script_writes_figure_origin_and_rerun_is_idempotent():
    Session = _session()
    _seed_doc(Session)
    with Session() as s:
        s.add(
            FigureAssetRow(
                figure_id="f1",
                doc_id="d1",
                locator={"doc_id": "d1"},
                image_hash="h1",
                rel_path="f1.png",
                width_px=10,
                height_px=10,
                figure_label="图1",
            )
        )
        s.add(
            EvidenceUnitRow(
                evidence_id="e1",
                doc_id="d1",
                locator={"doc_id": "d1", "format": "pdf"},
                text="图1展示了交易区间。",
                text_hash="e1",
                block_type="caption",
                figure_ids=["f1"],
            )
        )
        s.commit()

    first = link_figures_to_graph.link_figures(Session)
    second = link_figures_to_graph.link_figures(Session)

    assert first == second == (1, 1)
    assert _origins(Session) == {EdgeOrigin.figure_extract.value: 1}


def test_claim_extraction_writes_claim_origin(monkeypatch):
    Session = _session()
    _seed_doc(Session)
    monkeypatch.setattr(extract, "SessionLocal", Session)

    result = extract.ExtractionResult(
        claims=[extract.ExtractedClaim(subject="突破", predicate="常常失败")]
    )
    extract.persist_extraction("d1", "e1", result)
    extract.persist_extraction("d1", "e1", result)

    assert _origins(Session) == {EdgeOrigin.claim_extract.value: 1}
    with Session() as s:
        assert s.query(ClaimRow).count() == 1
