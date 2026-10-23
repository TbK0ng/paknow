"""Claim subject -> concept alignment: deterministic first, model only for the
residual and only above a confidence threshold. DB-backed tests use a temp
SQLite database and monkeypatch ``align.SessionLocal``.
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from paknow.db import Base
from paknow.db.models import (
    ClaimRow,
    DocumentRow,
    EvidenceUnitRow,
    GraphEdgeRow,
)
from paknow.graph import align


def _temp_session(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(align, "SessionLocal", Session)
    return Session


def _seed(Session, subjects: dict[str, str | None], *, edge: bool = False) -> None:
    """Create claims keyed by claim_id; value is the pre-existing concept id."""
    with Session() as s:
        s.add(
            DocumentRow(
                doc_id="d1",
                content_hash="h1",
                format="pdf",
                source_path="book.pdf",
            )
        )
        s.add(
            EvidenceUnitRow(
                evidence_id="e1",
                doc_id="d1",
                locator={"doc_id": "d1", "format": "pdf"},
                text="高二在强趋势中通常可靠。",
                text_hash="h-e1",
                block_type="paragraph",
            )
        )
        for claim_id, (subject, concept) in subjects.items():
            s.add(
                ClaimRow(
                    claim_id=claim_id,
                    subject=subject,
                    subject_concept_id=concept,
                    predicate="通常可靠",
                    qualifiers={"context": ["强趋势"], "modality": "usually"},
                    supporting_evidence=["e1"],
                    extraction_status="pending",
                )
            )
            if edge:
                s.add(
                    GraphEdgeRow(
                        edge_id=f"edge-{claim_id}",
                        from_node=claim_id,
                        to_node="e1",
                        relation="SUPPORTED_BY",
                        source="e1",
                        origin="claim_extract",
                    )
                )
        s.commit()


def test_exact_normalized_alias_resolves_first():
    alignment = align.align_subject("  High 2 ")

    assert alignment is not None
    assert alignment.concept_id == "high_2"
    assert alignment.method == "alias"


def test_single_concept_substring_resolves_deterministically():
    alignment = align.align_subject("20 EMA（动态磁铁）")

    assert alignment is not None
    assert alignment.concept_id == "ema"
    assert alignment.method == "substring"


def test_multiple_concepts_are_left_for_the_model():
    assert align.align_subject("K线2的双顶空头旗形") is None
    assert align.align_subject("1分钟图") is None


def test_model_below_threshold_writes_nothing(monkeypatch):
    Session = _temp_session(monkeypatch)
    _seed(Session, {"c1": ("K线2的双顶空头旗形", None)})
    monkeypatch.setattr(align, "_has_api_key", lambda: True)

    def fake_chat_json(schema, messages, **kwargs):
        return schema(alignments=[{"index": 0, "concept_id": "double_top", "confidence": 0.5}])

    monkeypatch.setattr(align.gateway, "chat_json", fake_chat_json)

    report = align.align_claims(apply=True, use_model=True, threshold=0.8)

    assert report.model_attempted is True
    assert report.newly_aligned == 0
    assert report.residual == 1
    with Session() as s:
        assert s.get(ClaimRow, "c1").subject_concept_id is None
        assert s.get(ClaimRow, "c1").extraction_status == "pending"


def test_model_is_skipped_without_an_api_key(monkeypatch):
    Session = _temp_session(monkeypatch)
    _seed(Session, {"c1": ("K线2的双顶空头旗形", None)})
    monkeypatch.setattr(align, "_has_api_key", lambda: False)

    def explode(*args, **kwargs):
        raise AssertionError("model must not be called without an API key")

    monkeypatch.setattr(align.gateway, "chat_json", explode)

    report = align.align_claims(apply=True, use_model=True)

    assert report.model_skipped == "no_api_key"
    assert report.model_attempted is False
    assert report.newly_aligned == 0


def test_model_above_threshold_aligns_the_residual_and_is_idempotent(monkeypatch):
    Session = _temp_session(monkeypatch)
    _seed(Session, {"c1": ("K线2的双顶空头旗形", None)})
    monkeypatch.setattr(align, "_has_api_key", lambda: True)
    calls = {"n": 0}

    def fake_chat_json(schema, messages, **kwargs):
        calls["n"] += 1
        return schema(alignments=[{"index": 0, "concept_id": "double_top", "confidence": 0.91}])

    monkeypatch.setattr(align.gateway, "chat_json", fake_chat_json)

    first = align.align_claims(apply=True, use_model=True, threshold=0.8)
    second = align.align_claims(apply=True, use_model=True, threshold=0.8)

    assert first.newly_aligned == 1
    assert first.by_method == {"model": 1}
    assert second.newly_aligned == 0
    with Session() as s:
        claim = s.get(ClaimRow, "c1")
        assert claim.subject_concept_id == "double_top"
        assert claim.extraction_status == "pending"


def test_deterministic_run_never_overwrites_and_keeps_claim_edges_and_evidence(monkeypatch):
    Session = _temp_session(monkeypatch)
    _seed(
        Session,
        {
            "c1": ("高二", None),
            "c2": ("20 EMA（动态磁铁）", None),
            "c3": ("K线2的双顶空头旗形", None),
            "c4": ("趋势", "trend"),
        },
        edge=True,
    )

    first = align.align_claims(apply=True)
    second = align.align_claims(apply=True)

    assert (first.originally_aligned, first.newly_aligned, first.residual) == (1, 2, 1)
    assert first.by_method == {"alias": 1, "substring": 1}
    assert second.newly_aligned == 0
    assert second.residual == 1
    with Session() as s:
        claims = {c.claim_id: c for c in s.query(ClaimRow).all()}
        assert claims["c1"].subject_concept_id == "high_2"
        assert claims["c2"].subject_concept_id == "ema"
        assert claims["c3"].subject_concept_id is None
        assert claims["c4"].subject_concept_id == "trend"  # never overwritten
        assert all(c.extraction_status == "pending" for c in claims.values())
        assert all(e.origin == "claim_extract" for e in s.query(GraphEdgeRow).all())
        assert s.query(EvidenceUnitRow).count() == 1
    assert [r["subject"] for r in first.residual_subjects] == ["K线2的双顶空头旗形"]
