# 04: `/query` 可选 debug 块

**What to build:** 排障时能从一次问答响应里直接看到链路内部，不必翻运行轨迹：命中及其分数与块类型、可引用 id 列表、上下文单元计数、充分性判定、失败清单、轮数、图谱统计。默认响应体保持精简、键集合不变。

**Blocked by:** 03

**Status:** resolved

- [x] 显式请求时响应增加 debug 块，含：命中（证据 id、分数、块类型）、可引用 id 列表、上下文单元计数、充分性判定、结构化失败清单、轮数、图谱统计。
- [x] 未请求时响应体不含该键，其余键集合与今天一致（有断言锁定，避免多传几十 KB）。
- [x] 失败清单是结构化的（分类 + 阶段），不是自由文本拼接；降级标记在其中可见。
- [x] 可引用边界不变：父单元与派生观察不出现在可引用 id 列表里（ADR-0002）。
- [x] debug 块只读，不额外落库、不改变既有运行轨迹的写入形状。

## Comments

- `QueryRequest` 新增 `debug: bool = False`；`apps/api/main.py:query` 只在为真时调用 `_debug_block(result)` 并挂上 `debug` 键。默认响应键集合与改动前逐字一致（15 个键），由 `test_the_debug_block_is_opt_in_and_leaves_the_other_keys_alone` 同时锁住「默认无 debug」与「两模式差值仅 debug」。
- `run_query` 的结果新增两个只读字段：`sufficiency`（`_check_node` 已写入 state 的判定，含 ticket 08 的 scale 字段仍只进轨迹）与 `context_unit_counts`（`total` / `parents` / `adjacent`，由 state 的上下文单元现算）。运行轨迹的 `save_trace` 入参与 `output` 形状未动。
- `debug` 块七项：`hits` 只投影 `{evidence_id, score, block_type}`（不复制正文）；`citable_ids` 直接复用 `run_query` 的 `citable_ids`（与生成端同一份 `citation_binding`，派生观察/图谱条目永不进入，ADR-0002 边界不变；父块路径仍是无数据的既有代码，仓库快照 0/1670 可解析，故列表无需第二套规则）；`context_unit_counts`；`sufficiency`；`failures`；`rounds`；`graph_stats`。
- `failures` 由 `_failure_event` 把既有轨迹字符串拆成 `{stage, category, detail}`：`generate: invalid_structure: …`、`chart_read: timeout (…)` 等复用 `timeout` / `invalid_structure` / `no_content` 分类，检索/图谱/扩展类失败记 `category: "error"`（它们没有模型输出分类）。降级答案因此以 `stage: "generate"` + 分类出现在清单里，同时响应顶层仍保留 ticket 03 的 `degraded` / `degradation`。
- 测试：`tests/test_api_chat.py` 新增 3 条（默认/显式键集合与断言、debug 内容与失败结构化、降级分类可见）；`tests/test_eval_answers.py` 新增 1 条验证 `run_query` 暴露判定与上下文计数且 `citable_ids` 无派生观察（`_stub_chain` 增加可选 `context_units`）。
- 冒烟（无 DB、无模型，桩 `run_query`）：默认 payload 15 键无 `debug`；`debug=True` 时键集合为原 15 键 + `debug`；打印的 debug 块含两条 hits（无正文）、`citable_ids: ["e1"]`、`context_unit_counts {total:4, parents:1, adjacent:3}`、判定、两条结构化失败（`error` / `invalid_structure`）、`rounds: 2`、`graph_stats`。
- 文档：`book/chapter8-service.md`（请求字段、响应键表、debug 字段说明、行号）与 `book/chapter10-examples.md`、`book/chapter5-query.md`、`book/chapter13-chart.md` 的引用行号更新。
