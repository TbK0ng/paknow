"""Post-process visual observations: strip the leaked chat-template prefix,
mark derived units, and re-index the cleaned text (no re-generation).

Why: the first VLM run stored the raw template (``user\n<prompt>\nassistant\n
<output>``) as the unit text; the ``assistant`` marker survives
``skip_special_tokens``, so strip at its last occurrence. The previous version
of this script committed outside the session context and never persisted.
"""
from __future__ import annotations

from qdrant_client.models import PointStruct, SparseVector

from paknow.config import settings
from paknow.db import SessionLocal, init_db
from paknow.db.models import EvidenceUnitRow, FigureAssetRow
from paknow.ids import content_hash
from paknow.retrieval.embedding import embed_texts
from paknow.retrieval.index import _point_id
from paknow.retrieval.qdrant_store import COLLECTION_TEXT, get_client
from paknow.retrieval.tokenize import sparse_indices_values

settings.ensure_dirs()
init_db()

_PROMPT_MARK = "你是价格行为图表的观察器"


def strip(text: str) -> str:
    idx = text.rfind("assistant")
    if idx >= 0:
        rest = text[idx + len("assistant") :].lstrip("\n :：-").strip()
        if rest:
            return rest
    return text


def reindex(units: list[EvidenceUnitRow]) -> None:
    client = get_client()
    for i in range(0, len(units), 32):
        batch = units[i : i + 32]
        dense = embed_texts([u.text for u in batch])
        points = []
        for j, u in enumerate(batch):
            idx, vals = sparse_indices_values(u.text)
            points.append(
                PointStruct(
                    id=_point_id(u.evidence_id),
                    vector={"dense": dense[j].tolist(), "sparse": SparseVector(indices=idx, values=vals)},
                    payload={
                        "evidence_id": u.evidence_id,
                        "doc_id": u.doc_id,
                        "block_type": "caption",
                        "text": u.text,
                        "figure_ids": u.figure_ids or [],
                        "chapter_path": u.chapter_path or [],
                        "pdf_page": u.pdf_page_1based,
                        "figure_label": u.figure_label,
                    },
                )
            )
        client.upsert(collection_name=COLLECTION_TEXT, points=points, wait=True)


cleaned: list[EvidenceUnitRow] = []
with SessionLocal() as s:
    # 1) strip polluted figure descriptions
    figs = (
        s.query(FigureAssetRow)
        .filter(FigureAssetRow.vlm_description.like(f"%{_PROMPT_MARK}%"))
        .all()
    )
    print(f"polluted figures: {len(figs)}", flush=True)
    for f in figs:
        f.vlm_description = strip(f.vlm_description)

    # 2) strip polluted observation units (and mark ALL observations derived)
    units = (
        s.query(EvidenceUnitRow)
        .filter(EvidenceUnitRow.text.like(f"%{_PROMPT_MARK}%"))
        .all()
    )
    print(f"polluted units: {len(units)}", flush=True)
    for u in units:
        new_text = strip(u.text)
        u.text = new_text
        u.text_hash = content_hash(new_text.encode("utf-8"))
        u.provenance = "derived_observation"
        cleaned.append(u)
    obs_clean = (
        s.query(EvidenceUnitRow)
        .filter(EvidenceUnitRow.text.like("%可见事实%"))
        .all()
    )
    for u in obs_clean:
        if u.provenance != "derived_observation":
            u.provenance = "derived_observation"
    s.commit()
    n_obs = len(obs_clean)

print(f"re-indexing {len(cleaned)} cleaned units...", flush=True)
reindex(cleaned)
print(f"CLEANED {len(cleaned)}; derived_observation total={n_obs}", flush=True)
