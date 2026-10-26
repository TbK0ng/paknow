# 02: 答案层评测冻结口径

**What to build:** 答案层评测新增冻结查询模式：用金题自带的问题与扩展查询构造查询计划，绕开现场路由，使同一份代码 + 同一份数据集跑两次得到的查询列表、轮数与覆盖率完全一致。运行记录写明计划来源与每题查询列表哈希，从而区分"可比"与"不可比"的两次运行。

**Blocked by:** 01

**Status:** resolved

- [x] 评测命令新增冻结模式开关；开启后每题的查询计划来自金题的问题与扩展查询，其余链路不变。
- [x] 逐题运行记录带计划来源与查询列表哈希；汇总记录带数据集哈希，数据集或计划被改动时可立刻发现。
- [x] 两次冻结模式运行：逐题查询列表、轮数、期望覆盖率完全一致；这条契约有可重复执行的检查（不调云模型）。
- [x] 不开该开关时行为与今天一致，仍记录路由产出的计划。
- [x] 生成端的降级标记（见 03）在记录里保持可读，便于门禁判定空答案。

## Comments

- `scripts/eval_answers.py --frozen-queries` builds each item's plan with `frozen_plan(item)` (`QueryPlan(sub_queries=expanded_queries[1:])`) and passes it as `run_query(..., plan=...)`; `expanded_queries[0]` is the question by dataset contract, so the frozen list is byte-identical to the gold `expanded_queries`. Without the flag `plan=None` and the router runs as before.
- Every record now carries `plan_source` (`frozen` / `routed`), `queries` and `query_hash` (from the chain's own provenance, so a routed run records the routed plan); error records keep the source with empty list/hash. `summary.json` adds `plan_source` next to the existing `dataset_sha256`, and the CLI prints both.
- Degradation stays readable: records carry `degraded` and `degradation` (ticket 03's `{"reason", "detail"}`) so the gate can reject placeholder answers without guessing from text length.
- Contract test `test_two_frozen_runs_agree_per_item_without_routing` runs two items × two arms twice through the real `run_query` with retrieval/generation stubbed and the router patched to fail; asserts identical `plan_source`/`queries`/`query_hash`/`routes`/`rounds`/`coverage`, and that `queries` equals the gold `expanded_queries`. No Qdrant, no model.
- Same smoke via the CLI (stubbed chain, `--limit 2 --frozen-queries`, two separate `--out` dirs): records identical, `plan: frozen dataset 7cf35d945cfc`.
- Left alone: `--resume` does not reject a checkpoint recorded with a different `plan_source`; the summary's `plan_source` makes the mismatch visible, but a resume across modes is a user error the flag does not block.
