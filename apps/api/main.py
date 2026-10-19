"""FastAPI service (plan §12.2)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from paknow.db import SessionLocal
from paknow.db.models import DocumentRow, EvidenceUnitRow, FigureAssetRow, RunTraceRow
from paknow.evidence.citations import resolve_citations
from paknow.memory.sop import list_sops
from paknow.retrieval.visual import mask_right_bytes
from paknow.workflows.graph import run_query

app = FastAPI(title="PAknow", version="0.1.0")

_VIEWER_INDEX = Path(__file__).resolve().parent.parent / "viewer" / "index.html"


class QueryRequest(BaseModel):
    query: str


def _resolve_answer(result: dict[str, Any]) -> dict[str, Any]:
    ans = result.get("answer") or {}
    citations = resolve_citations(ans.get("evidence_ids") or [])
    return {
        "run_id": result.get("run_id"),
        "intent": result.get("intent"),
        "is_framework": result.get("is_framework"),
        "routes": result.get("routes"),
        "answer": ans.get("answer"),
        "uncertainty": ans.get("uncertainty"),
        "citations": citations,
        "figures": result.get("figures"),
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/query")
def query(req: QueryRequest) -> dict[str, Any]:
    return _resolve_answer(run_query(req.query))


@app.post("/charts")
async def charts(query: str, file: UploadFile, cutoff: float = 0.0) -> dict[str, Any]:
    """Chart upload analysis. ``cutoff`` (0..1) masks the rightmost fraction
    of the image so bars after the analysis point are invisible (plan §8.4)."""
    data = await file.read()
    if cutoff > 0:
        data = mask_right_bytes(data, cutoff)
    return _resolve_answer(run_query(query, images=[data]))


@app.get("/evidence/{evidence_id}")
def evidence(evidence_id: str) -> dict[str, Any]:
    with SessionLocal() as s:
        u = s.get(EvidenceUnitRow, evidence_id)
        if u is None:
            raise HTTPException(404, "evidence not found")
        figs = (
            s.query(FigureAssetRow).filter(FigureAssetRow.figure_id.in_(u.figure_ids or [])).all()
            if u.figure_ids
            else []
        )
        doc = s.get(DocumentRow, u.doc_id)
    return {
        "evidence_id": u.evidence_id,
        "text": u.text,
        "block_type": u.block_type,
        "chapter_path": u.chapter_path or [],
        "pdf_page": u.pdf_page_1based,
        "printed_page": (u.locator or {}).get("printed_page_label"),
        "figure_label": u.figure_label,
        "epub_href": u.epub_href,
        "doc_id": u.doc_id,
        "doc_title": doc.title if doc else None,
        "figures": [
            {"figure_id": f.figure_id, "rel_path": f.rel_path, "label": f.figure_label}
            for f in figs
        ],
    }


@app.get("/sources/{doc_id}")
def source(doc_id: str) -> dict[str, Any]:
    with SessionLocal() as s:
        d = s.get(DocumentRow, doc_id)
        if d is None:
            raise HTTPException(404, "source not found")
        n_units = s.query(EvidenceUnitRow).filter(EvidenceUnitRow.doc_id == doc_id).count()
        n_figs = s.query(FigureAssetRow).filter(FigureAssetRow.doc_id == doc_id).count()
    return {
        "doc_id": d.doc_id,
        "title": d.title,
        "author": d.author,
        "translator": d.translator,
        "language": d.language,
        "format": d.format,
        "page_count": d.page_count,
        "entry_count": d.entry_count,
        "evidence_units": n_units,
        "figures": n_figs,
    }


@app.get("/runs/{run_id}")
def run(run_id: str) -> dict[str, Any]:
    with SessionLocal() as s:
        r = s.get(RunTraceRow, run_id)
        if r is None:
            raise HTTPException(404, "run not found")
        return {
            "run_id": r.run_id,
            "intents": r.intents,
            "routes": r.routes,
            "evidence_ids": r.evidence_ids,
            "output": r.output,
            "failures": r.failures,
            "elapsed_ms": r.elapsed_ms,
        }


@app.get("/sops")
def sops() -> list[dict[str, Any]]:
    return list_sops()


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(_VIEWER_INDEX)


@app.get("/sources/{doc_id}/page/{page}/image")
def page_image(doc_id: str, page: int, scale: float = 1.5) -> Response:
    """Render a PDF page to PNG for the local viewer."""
    import fitz  # PyMuPDF

    with SessionLocal() as s:
        doc = s.get(DocumentRow, doc_id)
        if doc is None or doc.format != "pdf":
            raise HTTPException(404, "PDF source not found")
        src = doc.source_path
    d = fitz.open(src)
    try:
        if page < 1 or page > d.page_count:
            raise HTTPException(404, "page out of range")
        pix = d[page - 1].get_pixmap(matrix=fitz.Matrix(scale, scale))
        return Response(content=pix.tobytes("png"), media_type="image/png")
    finally:
        d.close()
