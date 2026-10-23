# 05: 金标准回归

**What to build:** 20 条金标准在新链路上重跑，逐条给出「图独有」的判定与证据差异。要么用数字证明图谱扩展带来了文本召回拿不到的证据，要么用数字证伪。

**Blocked by:** 01, 04

**Status:** resolved

- [x] 使用 01 号票冻结的 6 条扩展查询重跑，不重新生成查询，保证前后可比。
- [x] 逐题输出：新增召回的期望证据、丢失的原有证据、因预算被截断的内容、以及该题是否满足「图独有」口径。
- [x] 「图独有」口径固定为：至少一条期望证据不在 6 条扩展查询的 top-20 并集内。
- [x] 汇总净增量；若没有净增量或出现回归，明确记录证伪结论与数字，不调口径迁就结果。
- [x] 回归结果与基线并列记录在特性目录中。
- [x] 文档与变更记录更新：图谱扩展的能力边界、预算与不可引用口径。

## Comments

Resolved on `feat/graph-expansion` (`d34c807`, docs `73d3188`, merged `ac00ebd`; review-fix rerun `e07e808`, merged `0fb9f53`).

- `scripts/eval_gold.py --mode graph` reuses the 20×6 frozen queries byte-identically (`3a0484a9…`); the baseline rerun reproduces 24/50, 10/20, 10/20.
- Per-question table + JSON: `.scratch/graph-expansion/gold-regression.{md,json}`; baseline remains `.scratch/graph-expansion/gold-baseline.{md,json}`.
- Result: expected hits 24 → 24 (net 0). Graph-attributable new expected evidence: 6 units on 5 questions. Lost baseline evidence: 6 (the new text path alone dropped 8; graph re-added 2). Graph-unique 10/20 by the fixed definition; 5/10 recovered. 20/20 hit `evidence ≤8`; edge/token budgets did not bind; 0 failures.
- Verdict: “graph recalls evidence outside the 6-query top-20 union” is supported (6 units / 5 questions); “graph expansion yields a net recall gain” is falsified (net 0). Metric and frozen queries unchanged.
- Docs updated: `book/chapter4-retrieval.md`, `chapter5-query.md`, `chapter12-allinrag-comparison.md`, `chapter10-examples.md` (budgets, non-citable/understanding-only, `graph_local` removal).
