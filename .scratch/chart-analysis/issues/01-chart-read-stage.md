# 01: 读图阶段与 `chart_read` 契约落地

**What to build:** 上传一张截图，流水线先做一次结构化读图，产出 `chart_read`（品种、周期、市场状态、摆动点、标注位、形态、不确定项），并把它送到 API 响应与运行轨迹里。答案是它读到的内容的证据，而不是一篇概念综述。此刻合成口径仍是单阶段（分层答案见 02），但「系统确实读过这张图」已经可验证。

**Blocked by:** None (can start immediately)

**Status:** resolved

- [ ] 图表问题在有图时先走一次读图调用，产出结构化 `chart_read`；读图阶段不接收证据块，因此结构上不产生引用编号
- [ ] `chart_read.levels` 逐条带 `source` 与 `basis`（图上依据），`price` 在任一位不确定时为 `null`，不按刻度推算
- [ ] 图上未印出的品种/周期/指标参数为 `null`，并在 `unreadable` 里各记一条
- [ ] `/charts` 响应含 `chart_read`；文本问答（无图）响应不含该字段
- [ ] `GET /runs/{id}` 的 `output` 里能查到同一次运行的 `chart_read`（JSON 列，无迁移）
- [ ] 读图调用恰好一次；读图内部失败时 `chart_read` 为 `null` 而流水线不中断
- [ ] 用真实截图人工核对：`levels` 中至少 10 条价位与图上标注一致（含末位），且标注原文（如 `2 @ 7519.75`）出现在 `annotations` 里
- [ ] 答案里没有任何引用编号指向读图内容（ADR-0002）

## Comments

**Resolved** in `fa272c3`, merged as `0ed6577`.

- `paknow/chart.py` 提供 `ChartRead`/`Swing`/`Level`/`Focus` 与 `read_chart`（一次调用、`timeout=120`、读图提示无证据块）。
- `generate()` 返回 `chart_read` + `chart_read_status`，`run_query` 透出；`/charts` 带这两个字段，`/query` 不带；`/runs/{id}` 的 `output` 直接可查（JSON 列，无迁移）。
- 真实截图（`aaa.png`，1516×5543，8 条带）实跑：`structured`，28–30 条 `levels`，含 `2 @ 7519.75`，`annotations` 保留标注原文；答案读图部分无 `[n]`。
- 已知：同一张长图偶发 `invalid_structure`（模型返回空 JSON），降级路径由 05 覆盖。
