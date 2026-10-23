# Claim → concept alignment

- database: `sqlite:///E:/PAknow/data/paknow.db`
- threshold: 0.8 (model pass)
- last script run was a no-op dry run (891 aligned / 0 new / 997 residual);
  the applied run history is recorded here by hand.

## Applied run history

| run | originally aligned | newly aligned | residual |
| --- | ---: | ---: | ---: |
| 1. deterministic dry run | 278 | 504 (planned) | 1106 |
| 2. deterministic `--apply` | 278 | **504** | 1106 |
| 3. deterministic `--apply` again (idempotency) | 782 | 0 | 1106 |
| 4. model `--apply --model --batch-size 40` | 782 | **109** | 997 |
| 5. deterministic `--apply` again (idempotency) | 891 | 0 | 997 |

Final state: 891 aligned / 1888 claims, 997 residual across 721 distinct subjects.
New alignments: **613** total (504 deterministic + 109 model).
Deterministic coverage: 501 distinct subjects / 782 claims (278 pre-existing + 504 new).
Model coverage: 77 distinct subjects / 109 claims, all above the 0.8 threshold;
no model errors, so the fail-open path was not exercised on this run.

By method (planned): alias 0, substring 504, model 109. Exact normalized alias
never fired because the pre-existing 278 already consumed every exact alias;
the deterministic gain came from single-concept substring matches.

## Invariants checked

- Claims remain `extraction_status='pending'` (one pre-existing `reviewed` claim
  untouched); nothing was promoted to citable evidence.
- `evidence_units` stayed at 91,235 rows; no evidence/provenance rows were added.
- All 1,888 `SUPPORTED_BY` edges remain `origin='claim_extract'`.
- No claim with an existing `subject_concept_id` was overwritten; a second
  `--apply` writes zero new alignments.
- Model-created concept nodes: 39 (139 → 178), so model-aligned ids are reachable
  by expansion seeding and taxonomy identity resolution (not every `auto_*` id is
  seed-reachable; see the review fix below).

## Identity resolution (review fix)

`align_subject` now resolves every dictionary match through
`identity.resolved_canonical` (the same helper taxonomy and expansion use) instead of
`find_terms(...).canonical`. A subject like `交易区间中的限价单` therefore aligns to
`trading_range` — the id expansion seeds actually query — instead of the
chapter-derived alias twin `auto_trading_range_tr`; and `--apply` updates the claim
rows that were planned (matched by `claim_id`), including NBSP/whitespace variants
that the old raw `subject` filter silently skipped. The model catalog only offers
identity-reachable ids for the same reason.

The historical run table above is unchanged (it records the applied runs); a fresh
dry run simply plans the remaining residual subjects through the corrected path.

## Mechanism query sample

```bash
PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db PYTHONPATH=. \
  .venv/Scripts/python.exe -c "
from paknow.graph.expansion import load_mechanism_candidates, seed_concepts
seeds = seed_concepts('扩大三角形')
for c in load_mechanism_candidates(seeds, limit=5):
    print(seeds, '|', c['from_label'], '->', c['to_label'], '|', c['claim']['qualifiers'], '|', c['evidence_id'])
"
```

Each row carries the claim's `qualifiers` and the `evidence_id`/page locator that
proves it; `render_graph_context` prints those conditions inline so a conditioned
claim is never shown as an unconditional rule.

The residual subject list is in `claim-residuals.json` (721 entries, with claim
counts, match reason, and dictionary candidates).
