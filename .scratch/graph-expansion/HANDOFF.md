# Handoff: graph expansion (tickets 04-05)

Session ended after tickets 01-03 were implemented, merged, and resolved. Continue on the integration branch; do not re-open 01-03.

## State

- Branch: `feat/graph-expansion` (integration). All ticket work is merged here.
- Resolved: `issues/01-retrieval-seam-and-gold-baseline.md`, `issues/02-concept-taxonomy-edges.md`, `issues/03-claim-concept-linking.md` (status + comments with numbers).
- Open frontier: `issues/04-graph-expansion-wiring.md` (blockers 01-03 satisfied), then `issues/05-gold-regression.md` (blocked by 04).
- Tests: `E:/PAknow/.venv/Scripts/python.exe -m pytest tests -q` → 23 passed.
- ADRs: `docs/adr/0002-derived-observations-are-not-citable-evidence.md`, `docs/adr/0003-graph-is-an-expansion-layer-not-a-scored-route.md`. Vocabulary: `GLOSSARY.md`.

## What already exists (use these, don't rebuild)

- Retrieval seam: `paknow/retrieval/pipeline.py` — `retrieve_evidence(query, queries, needs=, routes=, wants_examples=, acc=)` returns `RetrievalResult(text_hits, graph_evidence, graph_context, context_units, figures, toc_context, barrefs, failures, acc, query_count)`. `_retrieve_node` in `paknow/workflows/graph.py` is a thin adapter.
- Gold set: `evals/gold/graph_expansion.json` (20 x 6 frozen queries); baseline `scripts/eval_gold.py`; results `.scratch/graph-expansion/gold-baseline.{md,json}` (24/50 expected units found, 10/20 graph-unique candidates).
- Taxonomy: `paknow/graph/taxonomy.py` — `dedupe_headings`, `parse_heading`, `build_taxonomy_edges`, `rebuild_taxonomy`, `taxonomy_neighbors`; CLI `scripts/build_taxonomy.py`. Real DB: 328 edges (`heading_parse`), each with `source=evidence_id`.
- Claim alignment: `paknow/graph/align.py`, `scripts/align_claims.py`; real DB 891/1888 claims aligned, 997 residual.
- Edge provenance: `graph_edges.origin` ∈ `heading_parse|claim_extract|figure_extract|seed`; migration `scripts/migrate_graph_origin.py` already applied.
- Span-aware aliases: `paknow.retrieval.dictionary.find_terms(text)`.

## Ticket 04: what remains

Read `issues/04-graph-expansion-wiring.md` and the spec section "图谱扩展" + "提示词分区". Key current gaps:

1. `_retrieve_node` still runs the *legacy* graph recall (concept_lookup / transition_chains / expand_evidence_ids) and merges `graph_evidence` into `units` in `_generate_node`, making it citable. Replace this with expansion; graph evidence must never enter `evidence_ids`.
2. `generate()` still serializes `graph_context` as one JSON block. Replace with compact per-line entries carrying `evidence_id`, explicitly marked non-citable/understanding-only (same pattern as the 图读辅助 block).
3. `graph_local` is still in the router prompt (`paknow/workflows/router.py`) and in `QState.routes` checks. Remove it from the route vocabulary (ticket says rename/move out); routes are only independently scored channels.
4. Implement expansion: seeds from query dictionary hits (concurrent with text recall) + concepts in text hits (second pass); taxonomy 1 hop, mechanism chain 2 hops (concept -> aligned claim -> SUPPORTED_BY evidence); budgets concepts ≤8, edges ≤32, evidence ≤8, graph block ≤1,200 tokens; mechanism before taxonomy, relevance before edge count; graph evidence marked `provenance=graph` and deduped against text hits ("seen [n]"); only first round + gap rounds whose seeds contain new concepts; failures recorded, fail-open.
5. Hydrate evidence for taxonomy edges from `edge.source` (heading evidence) and for mechanism edges from `claim.supporting_evidence`; both need locators (chapter/page).

Suggested new module: `paknow/graph/expansion.py` (pure-ish planner + DB hydration), called from `retrieve_evidence` so tests can hit the seam. Tests: budgets/truncation priority, seen-evidence dedupe, non-citable partition, new-concept gating, fail-open. Smoke with a compositional question and confirm `answer.evidence_ids` contains no graph-only unit.

## Ticket 05: after 04

Extend `scripts/eval_gold.py` with a graph mode that uses the same frozen queries (never regenerate), reports per-question newly recalled expected evidence, lost baseline evidence, budget truncations, and the fixed "graph-unique" verdict (≥1 expected unit outside the 6-query top-20 union). Record baseline vs regression side by side in this directory; update `book/chapter4-retrieval.md`, `book/chapter5-query.md`, `book/chapter12-allinrag-comparison.md` (graph_local references), and the capability/budget/non-citable wording. If there is no net gain, record the falsification with numbers; do not change the metric.

## Environment

- Python: `E:/PAknow/.venv/Scripts/python.exe` (pytest installed). Run from repo root/worktree root.
- Real DB: `PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db` (worktrees have no `data/`).
- Scripts in a worktree need `PYTHONPATH=.`.
- Retrieval smoke needs Qdrant at `E:/PAknow/data/objects/qdrant` and the local BGE-M3 model; embedded Qdrant is single-process, so do not run two retrieval processes at once. In a worktree, either junction `data/` from the main repo (`cmd /c mklink /J E:\PAknow-wt\t04\data E:\PAknow\data`) or run the final smoke from `E:/PAknow`.
- Subagents: `task.agentModelOverrides` is saved globally to `deepseek/deepseek-v4-flash`; without it subagents fail with "No model selected".

## Worktrees

`E:/PAknow-wt/t02`, `E:/PAknow-wt/t03` are merged and can be removed (`git worktree remove ...`, `git branch -d ticket/...`). Create ticket 04 with:

```bash
git worktree add E:/PAknow-wt/t04 -b ticket/04-expansion feat/graph-expansion
```
