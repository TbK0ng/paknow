"""Visual observation index: observe figure assets via Qwen2.5-VL, store
derived observations as evidence units, and index them for the visual route."""
import time

from PIL import Image
from qdrant_client.models import PointStruct, SparseVector

from paknow.config import settings
from paknow.db import SessionLocal, init_db
from paknow.db.models import FigureAssetRow
from paknow.evidence.contracts import DocFormat, EvidenceUnit, Provenance, SourceLocator
from paknow.ids import content_hash, stable_id
from paknow.ingest.persist import persist_units
from paknow.ingest.store import object_path
from paknow.retrieval.embedding import embed_texts
from paknow.retrieval.index import _point_id
from paknow.retrieval.qdrant_store import COLLECTION_TEXT, ensure_collections, get_client
from paknow.retrieval.tokenize import sparse_indices_values
from paknow.retrieval.visual import observe_image

settings.ensure_dirs()
init_db()

with SessionLocal() as s:
    figs = (
        s.query(FigureAssetRow)
        .filter(
            FigureAssetRow.is_decorative.is_(False),
            FigureAssetRow.vlm_description.is_(None),
        )
        .all()
    )
print(f"figures (unobserved): {len(figs)}", flush=True)

obs_units: list[EvidenceUnit] = []
t0 = time.time()
for i, f in enumerate(figs):
    p = object_path(f.rel_path)
    if not p.exists():
        continue
    img = Image.open(p).convert("RGB")
    try:
        obs = observe_image(img, max_new_tokens=150)
    except Exception as e:  # noqa: BLE001
        print(f"  skip {f.figure_id[:8]}: {e}", flush=True)
        continue
    # update figure asset
    with SessionLocal() as s:
        row = s.get(FigureAssetRow, f.figure_id)
        row.vlm_description = obs
        row.description_model = settings.visual_model
        s.commit()
    # derived observation unit (never authoritative)
    loc = f.locator or {}
    obs_units.append(
        EvidenceUnit(
            evidence_id=stable_id("vobs", f.figure_id),
            doc_id=f.doc_id,
            locator=SourceLocator(
                doc_id=f.doc_id,
                format=DocFormat.pdf if f.pdf_page_1based else DocFormat.epub,
                pdf_page_1based=f.pdf_page_1based,
                epub_href=loc.get("epub_href"),
                figure_label=f.figure_label,
                chapter_path=loc.get("chapter_path", []),
            ),
            text=obs,
            text_hash=content_hash(obs.encode("utf-8")),
            block_type="caption",
            provenance=Provenance.derived_observation,
            figure_ids=[f.figure_id],
        )
    )
    if (i + 1) % 50 == 0:
        print(f"  {i+1}/{len(figs)} ({time.time()-t0:.0f}s)", flush=True)

print(f"observations: {len(obs_units)} in {time.time()-t0:.0f}s", flush=True)
persist_units(obs_units)

# index the observation units (dense + sparse)
print("indexing observations...", flush=True)
client = get_client()
ensure_collections(client)
points: list[PointStruct] = []
for i in range(0, len(obs_units), 32):
    batch = obs_units[i : i + 32]
    dense = embed_texts([u.text for u in batch])
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
                    "chapter_path": u.locator.chapter_path,
                    "pdf_page": u.locator.pdf_page_1based,
                    "figure_label": u.locator.figure_label,
                },
            )
        )
    client.upsert(collection_name=COLLECTION_TEXT, points=points, wait=True)
    points = []
print(f"INDEXED {len(obs_units)} observations", flush=True)
