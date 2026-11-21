"""A fresh PA evidence store can be bootstrapped without cloud or model weights."""
import fitz
import numpy as np
import pytest
from zipfile import ZipFile

import yaml
from qdrant_client import QdrantClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from paknow import db as database
from paknow.db import Base
from paknow.evidence import citations
from paknow.ingest import manifest, persist, pipeline
from paknow.ingest.epub import _parse_xhtml, parse_epub
from paknow.ids import file_hash, stable_id
from paknow.retrieval import index, pa_corpus, qdrant_store
from scripts import ingest_all, seal_pa_corpus


def test_missing_source_does_not_initialize_store(tmp_path, monkeypatch):
    def forbidden():
        pytest.fail("missing input must be rejected before opening the store")

    monkeypatch.setattr(ingest_all, "init_db", forbidden)
    with pytest.raises(SystemExit) as exc:
        ingest_all.main([str(tmp_path / "missing.pdf")])
    assert exc.value.code == 2


def test_existing_store_is_not_imported_over(tmp_path, monkeypatch):
    original = tmp_path / "example.pdf"
    original.touch()
    db = tmp_path / "occupied.db"
    db.write_bytes(b"existing store")
    engine = create_engine(f"sqlite:///{db}")
    try:
        monkeypatch.setattr(ingest_all, "engine", engine)
        monkeypatch.setattr(ingest_all.settings, "objects_dir", tmp_path / "objects")
        monkeypatch.setattr(ingest_all, "get_client", lambda: pytest.fail("must not open the existing store"))
        with pytest.raises(SystemExit) as exc:
            ingest_all.main([str(original)])
        assert exc.value.code == 2 and db.read_bytes() == b"existing store"
    finally:
        engine.dispose()


def test_wrong_original_hash_is_rejected_before_opening_store(tmp_path, monkeypatch):
    file = tmp_path / "book.pdf"
    file.write_bytes(b"different edition")
    engine = create_engine(f"sqlite:///{tmp_path / 'new-paknow.db'}")
    try:
        monkeypatch.setattr(ingest_all, "engine", engine)
        monkeypatch.setattr(ingest_all.settings, "objects_dir", tmp_path / "objects")
        monkeypatch.setattr(manifest, "expected_originals", lambda: {file.name: "0" * 64})
        monkeypatch.setattr(ingest_all, "get_client", lambda: pytest.fail("must not open Qdrant"))
        with pytest.raises(SystemExit) as exc:
            ingest_all.main([str(file)])
        assert exc.value.code == 2 and not (tmp_path / "new-paknow.db").exists()
    finally:
        engine.dispose()


def test_build_pdf_text_index_and_resolve_original(tmp_path, monkeypatch, capsys):
    pdf_path = tmp_path / "example.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 90), "Avoid entering a trading range without price confirmation.")
        pdf.save(pdf_path)
    engine = create_engine(f"sqlite:///{tmp_path / 'new' / 'metadata.db'}")
    vector = QdrantClient(":memory:")
    close = vector.close
    try:
        session = sessionmaker(bind=engine, expire_on_commit=False)
        for module in (database, citations, manifest, persist, pipeline, index, seal_pa_corpus):
            monkeypatch.setattr(module, "SessionLocal", session)
        for module in (database, ingest_all, seal_pa_corpus):
            monkeypatch.setattr(module, "engine", engine)
        monkeypatch.setattr(ingest_all, "init_db", lambda: Base.metadata.create_all(engine))
        monkeypatch.setattr(ingest_all.settings, "objects_dir", tmp_path / "objects")
        monkeypatch.setattr(ingest_all.settings, "models_dir", tmp_path / "models")
        for module in (manifest, seal_pa_corpus, pa_corpus):
            monkeypatch.setattr(module, "expected_originals", lambda: {pdf_path.name: file_hash(str(pdf_path))})
        monkeypatch.setattr(ingest_all.settings, "qdrant_url", None)
        monkeypatch.setattr(vector, "close", lambda: None)
        monkeypatch.setattr(seal_pa_corpus, "get_client", lambda: vector)
        monkeypatch.setattr(ingest_all, "get_client", lambda: vector)
        monkeypatch.setattr(index, "get_client", lambda: vector)
        monkeypatch.setattr(index, "embed_texts", lambda texts: np.ones((len(texts), qdrant_store.DENSE_DIM)))
        ingest_all.main([str(pdf_path)])
        assert "text index ready: 1 points" in capsys.readouterr().out
        points, _ = vector.scroll(qdrant_store.COLLECTION_TEXT)
        assert not vector.collection_exists("page_visual")
        hit, = citations.citation_ready_hits([points[0].payload])
        assert hit["pdf_page"] == 1 and hit["provenance"] == "native_text"
        assert "trading range" in hit["text"]

        graph_sources = tmp_path / "graph.yaml"
        graph_sources.write_text(yaml.safe_dump([{
            "concept": "breakout", "role": "confirmation", "profile": "any", "side": "any",
            "evidence": hit["evidence_id"], "sha256": hit["text_hash"], "question": "When is a break confirmed?",
        }]), encoding="utf-8")
        monkeypatch.setattr(pa_corpus, "PA_GRAPH_SOURCES", graph_sources)
        seed = seal_pa_corpus.seed_passages
        monkeypatch.setattr(seal_pa_corpus, "seed_passages", lambda: seed(graph_sources, session_factory=session))
        vector.set_payload(qdrant_store.COLLECTION_TEXT, {"block_type": "caption"}, [points[0].id])
        with pytest.raises(ValueError, match="index/SQLite mismatch"):
            seal_pa_corpus.seal()
        assert not (ingest_all.settings.objects_dir / pa_corpus.MANIFEST).exists()
        vector.set_payload(qdrant_store.COLLECTION_TEXT, {"block_type": hit["block_type"]}, [points[0].id])
        report = seal_pa_corpus.seal()
        assert report["reviewed_leads"] == report["dense_points"] == 1
        pa_corpus.check_corpus()
    finally:
        close()
        engine.dispose()


def test_epub_keeps_caption_and_anchor_without_opening_image():
    html = '''<body><p>First.</p><div class="figure"><img src="not-present.png"/>
        <div class="caption"><a id="example"></a>Figure 17.2: A false breakout.</div></div>
        <p>Next.</p></body>'''
    first, caption, last = _parse_xhtml(html, "chapter.xhtml", "book", ["Chapter"])
    assert first.evidence_id == stable_id("ev", "book", "chapter.xhtml", "0")
    assert last.evidence_id == stable_id("ev", "book", "chapter.xhtml", "1")
    assert caption.block_type == "caption" and caption.provenance.value == "native_text"
    assert caption.locator.figure_label == "Figure 17.2" and caption.locator.epub_fragment == "example"
    assert not hasattr(caption, "figure_ids")


def test_epub_nested_ncx_locates_each_section_in_reading_order(tmp_path):
    file = tmp_path / "two.epub"
    with ZipFile(file, "w") as archive:
        archive.writestr("OPS/content.opf", '<package xmlns="http://www.idpf.org/2007/opf"><manifest>'
                         '<item id="text" href="chapter.xhtml"/></manifest><spine><itemref idref="text"/></spine></package>')
        archive.writestr("OPS/toc.ncx", '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>'
                         '<navPoint><navLabel><text>Chapter</text></navLabel><content src="chapter.xhtml"/>'
                         '<navPoint><navLabel><text>First</text></navLabel><content src="chapter.xhtml#a"/></navPoint>'
                         '<navPoint><navLabel><text>Second</text></navLabel><content src="chapter.xhtml#b"/></navPoint>'
                         '</navPoint></navMap></ncx>')
        archive.writestr("OPS/chapter.xhtml", '<body><p>Intro.</p><h2 id="a">First</h2><p>First text.</p>'
                         '<h2><a id="b"></a>Second</h2><p>Second text.</p></body>')
    parsed = parse_epub(str(file), "doc")
    assert [u.locator.chapter_path for u in parsed.evidence_units] == [
        ["Chapter"], ["Chapter", "First"], ["Chapter", "First"], ["Chapter", "Second"], ["Chapter", "Second"]]
    assert all(u.locator.epub_href == "OPS/chapter.xhtml" for u in parsed.evidence_units)
