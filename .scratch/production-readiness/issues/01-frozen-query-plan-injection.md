# 01: 冻结查询注入口与查询溯源

**What to build:** 生产入口接受一份外部注入的查询计划，跳过路由模型但走完其余整条链路（查询装配 6 槽、召回、重排、多样性、图谱扩展、生成、记录）。结果与运行轨迹写明这份计划从哪来（冻结 / 路由）以及最终装配出的查询列表及其哈希，使两次同输入的运行能逐题逐字比较。

**Blocked by:** None (can start immediately)

**Status:** resolved

- [x] 注入计划时路由节点不调用模型；意图、是否综述机制、路由通道、证据需求、SOP 派生态与"路由得到同一份计划"时完全一致。
- [x] 注入的计划与路由产出的计划走同一条装配路径，因此注入不会让最终查询列表超过 6 槽，也不会出现"注入后比路由多/少几条"。
- [x] 结果与运行轨迹都带计划来源标记；运行轨迹的请求 JSON 携带来源与查询列表哈希，无需库表迁移。
- [x] 装配后的查询列表可以从结果里读到（能直接算哈希、能人眼核对），稳定哈希对同一列表恒定。
- [x] 同一输入两次调用，逐题查询列表相同（确定性契约；用假网关验证，不调云模型）。
- [x] 不注入计划时行为与今天一致：仍由路由生成计划。

## Comments

Resolved on `feat/pr-01-plan-injection`.

- `run_query(..., plan=QueryPlan)` is the single seam: `_route_node` takes `state["plan"]` when present, sets `plan_source="frozen"` (else `"routed"`), derives the same ten state fields plus `round=0` and the same `select_sop`/`apply_sop` path, and never calls `route()`. No new node, no conditional edge.
- `_retrieve_node` records the assembled list once (`state["queries"] = queries[:6]`) after the `next_query` / SOP-extra / plan splicing, so a later gap round cannot rewrite it; the recorded list is exactly what `retrieve_evidence` submits as its first six. The argument handed to the pipeline is unchanged, so graph seeding sees the same input as before.
- `graph.query_hash()` is one module-level SHA-256 over the JSON-encoded list (order-sensitive, no new module). `run_query` returns `plan_source`, `queries` and `query_hash`; `save_trace` writes `plan_source` + `query_hash` into `RunTraceRow.request` (JSON column, no migration; `optimizer` still reads `request["query"]`). The HTTP layer is untouched: it never injects a plan and its default body stays lean.
- Tests (`tests/test_plan_injection.py`, 7): injected plan skips the router (patched to raise) while the rest of the chain runs; injected vs gateway-routed identical plan compared on observed node inputs (retrieve kwargs, sufficiency needs incl. SOP requirements, generate kwargs) and on the returned query list/hash/rounds; six-slot cap observable from the result; hash stable and order-sensitive; trace carries source+hash; two identical frozen runs agree; no plan still routes exactly once.
- Smoke (temp SQLite, real `save_trace`, no cloud): frozen plan → `queries` = 问题 + step_back + 2 sub-queries + hyde, `query_hash` `5cdee26e…` on both runs, `rounds=1`, and both `run_traces.request` rows carried `plan_source="frozen"` with the same hash.
- Deferred (ticket 02's scope): building a plan from gold items, the `--frozen-queries` switch and dataset hash.
