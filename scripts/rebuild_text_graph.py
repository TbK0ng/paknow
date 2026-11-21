"""Build the PA text graph from the ingested corpus and checked original passages."""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import yaml

from paknow.db import SessionLocal
from paknow.db.models import ClaimRow, EvidenceUnitRow, GraphEdgeRow, GraphNodeRow
from paknow.graph.identity import canonical_for, concept_kind_for, concept_node_id, label_for
from paknow.graph.pa_context import ROLE_PROFILES, ROLE_RELATIONS, SCOPE, valid_relation
from paknow.ids import stable_id
from paknow.retrieval.pa_corpus import PA_GRAPH_SOURCES


def seed_passages(path: Path = PA_GRAPH_SOURCES, *, session_factory=None) -> int:
    entries = yaml.safe_load(path.read_text(encoding="utf-8"))
    factory = session_factory or SessionLocal
    with factory.begin() as session:
        old = session.query(ClaimRow).filter(ClaimRow.source_scope == SCOPE)
        old.delete(synchronize_session=False)
        session.query(GraphEdgeRow).filter(GraphEdgeRow.origin == SCOPE).delete(synchronize_session=False)
        concepts = set()
        for entry in entries:
            concept = canonical_for(entry["concept"])
            eid = entry["evidence"]
            row = session.get(EvidenceUnitRow, eid)
            if (concept is None or row is None or row.provenance not in {"native_text", "ocr"}
                    or not row.text.strip() or row.child_ids or row.block_type in {"footer", "heading"}
                    or sha256(row.text.encode("utf-8")).hexdigest() != entry["sha256"]):
                raise ValueError(f"invalid concept or changed original for PA graph source: {eid}")
            node_id = concept_node_id(concept)
            if concept not in concepts:
                session.merge(GraphNodeRow(node_id=node_id, node_type="concept", concept_id=concept,
                                           label=label_for(concept), concept_kind=concept_kind_for(concept)))
                concepts.add(concept)
            role, profile, side = (entry[key] for key in ("role", "profile", "side"))
            if profile not in ROLE_PROFILES.get(role, ()) or side not in {"any", "long", "short"}:
                raise ValueError(f"invalid PA role/profile/side: {role}/{profile}/{side}")
            relation = entry.get("relation", ROLE_RELATIONS[role])
            if not valid_relation(role, profile, relation) or not entry["question"].strip():
                raise ValueError(f"invalid relation or question for PA role {role}: {relation}")
            cid = stable_id("claim", SCOPE, concept, role, profile, side, eid)
            session.add(ClaimRow(claim_id=cid, subject=label_for(concept), subject_concept_id=concept,
                                 predicate=entry["question"],
                                 qualifiers={"role": role, "profile": profile, "side": side, "sha256": entry["sha256"],
                                             "relation": relation, "condition": entry.get("condition", "")},
                                 supporting_evidence=[eid],
                                 source_scope=SCOPE, extraction_status="reviewed"))
            for frm, to, rel in ((node_id, cid, relation), (cid, eid, "SUPPORTED_BY")):
                session.add(GraphEdgeRow(edge_id=stable_id("edge", frm, to, rel, SCOPE), from_node=frm,
                                         to_node=to, relation=rel, source=eid,
                                         origin=SCOPE, extraction_status="reviewed"))
    return len(entries)
