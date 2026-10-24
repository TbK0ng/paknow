# Handoff: graph expansion (complete)

The graph-expansion spec and tickets 01–05 are implemented, merged, reviewed, and resolved on the integration branch. Do not re-open them; this file records the shipped state and how to verify it.

## State

- Branch: merged into `main` (`0fb9f53` is an ancestor of `main`); `feat/*` branches and worktrees deleted.
- Resolved: `issues/01-retrieval-seam-and-gold-baseline.md` … `issues/05-gold-regression.md`; spec `spec.md`.
- Tests: `E:/PAknow/.venv/Scripts/python.exe -m pytest tests -q` → **323 passed**.
- Review: `.scratch/graph-expansion/code-review.md`; all findings fixed in `e07e808`, merged `0fb9f53`.
- ADRs: `docs/adr/0002-derived-observations-are-not-citable-evidence.md`, `docs/adr/0003-graph-is-an-expansion-layer-not-a-scored-route.md`. Vocabulary: `GLOSSARY.md`.
- Worktrees `E:/PAknow-wt/{t04,t05,t-review}` and their `ticket/*` branches were removed after merge.

## Shipped architecture

### Retrieval seam

`paknow/retrieval/pipeline.py` — `retrieve_evidence(query, queries, needs=, routes=, wants_examples=, acc=, prior_graph_evidence=)` returns `RetrievalResult`:

- `text_hits`: scored text recall; the only rank order the answer uses.
- `graph_evidence`: expansion entries accumulated across rounds; `provenance="graph"`, `non_citable=True`; never merged into `citable_units`.
- `citable_units` / `citation_index`: exactly what generation renders and the evidence-id → `[n]` binding (derived observations skipped). Generation consumes this index instead of re-deriving it.
- `context_units`, `figures`, `toc_context`, `barrefs`: auxiliary recall.
- `failures`: non-fatal failures; retrieval never raises for one bad step.
- `expanded_concepts`, `graph_stats`, `query_count`: diagnostics (`graph_stats` is always populated, including `seeds: 0`).

`_retrieve_node` in `paknow/workflows/graph.py` is a thin adapter: it forwards `prior_graph_evidence`, stores the accumulated result, and never writes `[]` on a round without new concepts.

### Expansion

`paknow/graph/expansion.py`:

- Seeds: query dictionary hits (concurrent with text recall) plus concepts found in returned evidence (second pass). Only new concepts expand; the same concept is never expanded twice.
- Depth: taxonomy 1 hop; mechanism chain concept → aligned claim → `SUPPORTED_BY` evidence, 2 hops.
- Budgets: concepts ≤8, traversed candidate edges ≤32 (mechanism before taxonomy), evidence ≤8, graph block ≤1,200 tokens. Edge truncation counts candidates, so `truncated_edges` can bind.
- Hydration: taxonomy entries from `edge.source`, mechanism entries from `claim.supporting_evidence`, with chapter/PDF/printed/EPUB locators. Text-seen entries render as `已见 [n]` without duplicate body.
- Fail-open: every failure is appended to `failures` and generation continues.

### Prompt

`paknow/workflows/generate.py` renders `graph_evidence` as compact per-line entries with `evidence_id` and an explicit `不可引用·仅理解辅助` marker. Claim conditions/qualifiers/exceptions are printed inline; conditioned claims are not rendered as unconditional. Order stays: 目录框架 → 图谱上下文 → 图读辅助 → 证据.

### Routes

`paknow/workflows/router.py`: `ROUTE_VOCAB = ("hybrid_text", "toc", "visual_page", "numeric")`; `graph_local` is retired and filtered if a model emits it. If filtering empties the list, the router falls back to `["hybrid_text"]`.

## Data / build state

- Taxonomy (`scripts/build_taxonomy.py`): 16,644 headings loaded, 12 TOC titles excluded, 6,245 after dedupe (headers 8,880, duplicates 1,507), 994 parsed (15.92% coverage), **328 edges**, fingerprint `b0a9aff4036c7c18`. TOC paragraph rows never enter the heading scan; TOC titles are explicitly excluded and counted for coverage. Repeat `--apply` is idempotent.
- Claim alignment (`scripts/align_claims.py`): 891/1,888 aligned, 997 residual across 721 subjects. Alignments resolve through `identity.resolved_canonical` (same identity as taxonomy/expansion), and `--apply` updates the planned `claim_id` rows (including NBSP/whitespace variants). The 19 pre-existing rows that still store a twin id are intentionally not overwritten; that is a data migration, not a code fix.
- Edge provenance: `EdgeOrigin` enum in `paknow/graph/contracts.py` (`heading_parse|claim_extract|figure_extract|seed`); all producers (taxonomy, claims, figures, seed script) write it. `scripts/migrate_graph_origin.py` was applied.

## Gold regression

Artifacts: `.scratch/graph-expansion/gold-baseline.{md,json}` and `gold-regression.{md,json}`. Frozen queries `evals/gold/graph_expansion.json` are byte-identical to ticket 01 (`3a0484a9…`); the fixed metric is unchanged.

| Metric | Baseline | Regression |
| --- | ---: | ---: |
| Expected units found | 24/50 | 24/50 |
| Text-path units | 24 | 16 |
| Graph-attributable new | — | 6 (5 questions) |
| Lost baseline evidence | — | 6 |
| Graph-unique questions | 10/20 | 10/20 (5/10 recovered) |
| Evidence-budget truncations | — | 20/20 |
| Edge/token truncations | — | 0 |
| Failures | — | 0 |

Verdict: graph expansion recalls evidence outside the 6-query top-20 union (supported: 6 units / 5 questions), but the run shows **no net recall gain** (net 0) because the new text path (top-40 → rerank 20 → diversify 14) drops 8 baseline units and graph re-adds 2. This is a recorded falsification, not a metric change.

## Verification

```bash
# tests
E:/PAknow/.venv/Scripts/python.exe -m pytest tests -q

# baseline (frozen queries; embedded Qdrant is single-process, run sequentially)
PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db OBJECTS_DIR=E:/PAknow/data/objects PYTHONPATH=. \
  python scripts/eval_gold.py --mode baseline

PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db OBJECTS_DIR=E:/PAknow/data/objects PYTHONPATH=. \
  python scripts/eval_gold.py --mode graph

# taxonomy rebuild
PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db PYTHONPATH=. \
  python -m scripts.build_taxonomy          # dry run
```

Ticket 04 smoke: gold `mech-07` (`突破在5分钟图上常见吗，什么样的突破才算强？`) → 14 text hits / 18 citable, 8 graph entries (7 graph-only, including expected `964597e4…`), graph block present, 17 citable citation ids and zero graph-only ids in `evidence_ids`.
