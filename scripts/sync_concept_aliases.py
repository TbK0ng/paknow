"""Sync the term dictionary (terms.yaml) into the concept_alias table.

The alias table is the persistent, reviewable bilingual concept index used by
graph retrieval; the YAML stays the human-editable source (plan §5.5, §6.2).
"""
from __future__ import annotations

from paknow.config import settings
from paknow.db import SessionLocal, init_db
from paknow.db.models import ConceptAliasRow
from paknow.retrieval.dictionary import load_terms

settings.ensure_dirs()
init_db()

with SessionLocal() as s:
    n = 0
    for term in load_terms():
        s.merge(
            ConceptAliasRow(
                canonical_concept=term.canonical,
                aliases=term.aliases,
                ambiguity_type=term.ambiguity,
                context=term.context,
                review_status="pending",
            )
        )
        n += 1
    s.commit()
print(f"synced {n} concepts into concept_aliases")
