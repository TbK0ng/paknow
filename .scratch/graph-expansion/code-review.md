# Code review: feat/graph-expansion vs e06b7a4

Fixed point `e06b7a4`; integration tip `ac00ebd`. Two axes, deliberately separate.

## Standards

### Hard violations

1. `paknow/graph/align.py`, `align_subject` (l.90-99) — violates `identity.py`'s stated rule ("terms.yaml is the only source of identity", surface forms resolve via `canonical_for`). `concepts = {m.canonical for m in find_terms(normalized)}` bypasses the identity helper that `taxonomy._match_concept` and `expansion._resolved` both use. Verified: `align_subject("交易区间中的限价单")` returns `auto_trading_range_tr`, but expansion seeds resolve `交易区间` to `trading_range`, so those aligned claims can never be retrieved by `load_mechanism_candidates`.
   Same file, `align_claims` apply (l.~263): keys come from `normalize_subject(...)` but the UPDATE filters `ClaimRow.subject == subject`. Verified with an NBSP subject: dry run plans 1 alignment, `--apply` writes 0 (claim stays `NULL`). Also `normalize_subject` duplicates `taxonomy.normalize_heading` verbatim.

2. `paknow/db/models.py:146-148` + `graph/contracts.py:99`, cutover gap — origin is added with default `"claim_extract"`, but existing writers remain unmigrated: `scripts/seed_and_extract.py:52,63` (`source="seed"` PART_OF/TRANSITIONS_TO) and `scripts/link_figures_to_graph.py:32` (`ILLUSTRATED_BY`) never pass `origin`. New seed/figure edges therefore get `origin='claim_extract'`, contradicting the column comment, `contracts.py` comment, and `migrate_graph_origin.py`'s origin mapping. `build_taxonomy.py`'s origin report and any future origin-filtered rebuild then misclassify them.

### Judgement calls

3. `paknow/graph/expansion.py:168-200`, Speculative Generality — `plan_context(seeds, ...)` never uses `seeds`; `edges` increments exactly with `plan.entries`, so `edges >= max_edges` is unreachable under shipped defaults (`MAX_EVIDENCE=8 < MAX_EDGES=32`); `truncated_edges` is permanently false, so the documented 边≤32 budget (book ch.4 §7.2) can't bind.

4. Duplicated Code / Shotgun Surgery — `TAXONOMY_RELATIONS` exists twice with divergent types (`taxonomy.py:45` `tuple[Relation]`, `expansion.py:36` string tuple); `ORIGIN_HEADING/CLAIM/SEED` constants are bypassed by raw `"claim_extract"` (`expansion.py:290`) and inline SQL in `migrate_graph_origin.py:48-53`.

5. `paknow/retrieval/pipeline.py:44-45,191-192` (+ `workflows/graph.py:55,113`), Mysterious Name/Duplicated Code — `graph_evidence` and `graph_context` are two names for the identical list; only `eval_gold.py` reads `graph_evidence`, nothing reads it in the workflow, and chapter 12 still cites it as a distinct artifact.

6. `graph/contracts.py:99`, Primitive Obsession — `origin: str` with a comment enumerating four variants, in a module that models every other such concept (`Relation`, `NodeType`, `ConceptKind`) as an enum.

No ADR-0001/0002/0003 violations found.

## Spec

### (a) Missing / partial

1. **Graph context is erased on gap rounds.** Ticket 04: "只在首轮与「出现新概念的缺口轮」执行；同一概念不重复展开"; story 12: "缺口轮只为新出现的概念增量补跑". The seam returns only the current round's entries and the adapter overwrites state (`paknow/workflows/graph.py:104-114`, `paknow/retrieval/pipeline.py:184-192`). A second round with a new seed replaces round 1's block; a round with no new seed sets it to `[]`. Incremental expansion must accumulate, not replace. P1, confidence 0.75.

2. **Edge origin is only backfilled once, not written by the other producers.** Story 9: "每条边记录来源（标题解析 / 主张抽取 / 人工种子）…按来源关停或重建". `origin` defaults to `claim_extract` (`paknow/db/models.py:148`); `scripts/seed_and_extract.py:52,63,103` and `scripts/link_figures_to_graph.py:32` still create seed/figure edges without `origin`, so any re-ingest relabels them `claim_extract`. P2, 0.85.

3. **Rule "开头约 10 页的目录表格…不参与主抽取" is not implemented.** `load_heading_units` (`paknow/graph/taxonomy.py:257-266`) filters only `block_type == "heading"`; no front-matter/TOC exclusion or coverage cross-check exists, and `taxonomy-build.md` records none. P3, 0.5.

4. **Ticket/handoff bookkeeping is stale.** Orchestrator handles ticket status; do not edit ticket status files. (HANDOFF update also orchestrator-owned.)

5. Ticket 04's acceptance "用一次实际查询验证：…答案的引用只指向原文证据" has no recorded run in the ticket; orchestrator appends the implementer's recorded smoke. (No code action.)

### (b) Scope creep

No material creep. `paknow/gateway.py` lazy client and `pyproject.toml` pytest config are unrequested but incidental; `graph/retrieve.py` deletion is clean cutover.

### (c) Implemented but wrong

1. **Conditioned claims lose their condition in the prompt.** Story 6: "问…在什么条件下…时拿到带条件的主张". Expansion hydrates `claim.qualifiers` (`expansion.py:288`) but `render_graph_context` prints only `subject → predicate` (`generate.py:88-96`), so "突破 → 常常失败" appears unconditional. P2, 0.7.

2. **The 32-edge budget can never bind.** `expansion.py:186-201` increments `edges` only per selected entry, and `max_evidence` (8) stops first; `truncated_edges` is unreachable in production (0/20 in the regression), contrary to "边 ≤32 … 超限时…截断". P3, 0.8.

## Merger-noticed risks (also fix if real)

- `pipeline.py` populates `graph_stats` only when `new_seeds` is non-empty → `{}` on pure cache-hit calls; eval comparisons can see spurious `graph_stats` deltas.
- `seen_index` numbering assumes `generate()` filters `derived_observation` before numbering exactly as `pipeline` does; two copies of the same ordering rule.
- Router silently filters unknown route labels; model output of only `graph_local` yields empty `routes` rather than falling back to hybrid text.
- `seed_concepts` runs in a single worker thread while `hybrid_search` runs on the main thread; safe for SQLite-only dictionary path, fragile if it gains Qdrant access.
