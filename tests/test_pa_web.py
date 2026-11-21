"""PA-only web surface and in-page source details."""
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.api.main import app
from paknow.db import Base
from paknow.db.models import DocumentRow, EvidenceUnitRow
from paknow.evidence import citations


def test_root_serves_only_pa_agent():
    client = TestClient(app)
    assert client.get("/", follow_redirects=False).headers["location"] == "/pa-agent"
    page = client.get("/")
    assert page.status_code == 200
    assert "<h1>PA Agent</h1>" in page.text
    assert "返回 PAknow" not in page.text
    assert client.get("/health").json() == {"status": "ok"}
    for path in ("/query", "/search", "/charts", "/sops", "/docs", "/redoc", "/openapi.json",
                 "/documents", "/evidence/x", "/sources/x", "/runs/x", "/figures/x/image"):
        assert client.post(path).status_code == 404
        assert client.get(path).status_code == 404


def test_expand_source_excludes_derived_observations(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'metadata.db'}")
    try:
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)
        with session.begin() as db:
            db.add(DocumentRow(doc_id="book", content_hash="hash", format="pdf", source_path="book.pdf", title="原书"))
            for eid, provenance in [("vision", "derived_observation"), ("page", "native_text"), ("scan", "ocr")]:
                db.add(EvidenceUnitRow(evidence_id=eid, doc_id="book", locator={}, text=f"原文 {eid}",
                                       text_hash=eid, provenance=provenance, chapter_path=["回调"],
                                       pdf_page_1based=10, printed_page_label="6"))
        monkeypatch.setattr(citations, "SessionLocal", session)
        monkeypatch.setattr("apps.api.pa_agent.corpus", lambda: ("test", frozenset({"book"})))
        client = TestClient(app)
        prefix = "/pa-agent/evidence/"
        assert client.get(prefix + "page").json() == {
            "label": "《原书》 → 回调 → PDF 第 10 页（章内印刷页 6）",
            "text": "原文 page",
            "provenance": "native_text",
        }
        assert client.get(prefix + "scan").json()["provenance"] == "ocr"
        assert client.get(prefix + "vision").status_code == 404
        assert client.get(prefix + "missing").status_code == 404
    finally:
        engine.dispose()
