"""Graph extraction over the high-value subset (captions + figure-linked explanations)."""
import time

from sqlalchemy import func

from paknow.db import SessionLocal
from paknow.db.models import EvidenceUnitRow
from paknow.graph.extract import extract_from_text, persist_extraction

with SessionLocal() as s:
    units = (
        s.query(EvidenceUnitRow)
        .filter(
            (EvidenceUnitRow.block_type == "caption")
            | (func.json_array_length(EvidenceUnitRow.figure_ids) > 0)
        )
        .all()
    )

# dedup + cap
seen: set[str] = set()
uniq = []
for u in units:
    if u.evidence_id in seen or len(u.text.strip()) < 20:
        continue
    seen.add(u.evidence_id)
    uniq.append(u)

print(f"high-value units: {len(uniq)}", flush=True)
n_concepts = n_claims = 0
t0 = time.time()
for i, u in enumerate(uniq):
    r = extract_from_text(u.text)
    persist_extraction(u.doc_id, u.evidence_id, r)
    n_concepts += len(r.concepts)
    n_claims += len(r.claims)
    if (i + 1) % 50 == 0:
        print(f"  {i+1}/{len(uniq)} concepts={n_concepts} claims={n_claims} ({time.time()-t0:.0f}s)", flush=True)
print(f"DONE concepts={n_concepts} claims={n_claims} in {time.time()-t0:.0f}s", flush=True)
