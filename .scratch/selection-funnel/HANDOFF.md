# HANDOFF — 选材漏斗（selection-funnel）

**状态：代码完成、评测跑完、结论已写进 `spec.md`。** 本文件写给下一个会话，不需要回忆对话。

## 结论一句话

选材层改动让召回 41→50（文本臂）、55→63（图谱臂），答案覆盖率 0.2333→0.30~0.35（跨运行有噪声）；判定分相对旧答案低约 0.2，**无法归因到任何已改的旋钮**，且答案层评测本身非确定性，继续追没有意义。父块不可引用已上线：引用里的父块 64→0、同文重复引用 39 组→0。

## 改动清单（勿重复实现）

| 改动 | 位置 | 测试 |
| --- | --- | --- |
| 重排按"真正召回它的查询"打分，取最高分 | `paknow/retrieval/search.py:82` `rerank_best` | `tests/test_retrieval_pipeline.py` |
| 重排池 20 → 80 | `paknow/retrieval/pipeline.py:46` `_RERANK_POOL` | 同上 |
| 去重键 80 → 200 字；每书配额 5 → 8 | `paknow/retrieval/search.py:136` `diversify` | 同上 |
| 父块只作上下文、不可引用（无编号块） | `pipeline.py:68` `citable_units`、`generate.py:262` `render_reading_context`、`graph.py:213` | `tests/test_reading_context.py` |
| JSON 解析容忍裸控制字符 | `paknow/gateway.py:164` `json.loads(..., strict=False)` | `tests/test_gateway_json.py` |
| 父块回填（743 个父块） | `scripts/backfill_parent_chunks.py` | `tests/test_chunk_units.py` |

`pytest tests -q` → 343 passed；`python scripts/check_scratch.py` → OK。

## 确定的数字（冻结查询，可宣称）

| 口径 | 旧 | 新 |
| --- | ---: | ---: |
| 文本臂召回（v2 金题 60 题 / 123 点） | 41 | **50** |
| 图谱臂召回 | 55 | **63** |
| 完全命中题 | 20 | 25 |

## 产物

| 项 | 路径 |
| --- | --- |
| 召回 | `.scratch/selection-funnel/gold-after{,-2}-text-w14.json`、`gold-after-graph-w14.json` |
| 答案层 ×3 | `answers{,_2,_3}/20261025…/` |
| 判定 ×3 + 漂移对照 | `judgements{,2,3}/…`、`judgements-drift/20261025T054704Z` |

## 重要教训（别再踩）

- **答案层评测不是确定性的**：同一批题两次运行，`routes` 只有 61/120 相同、20 条覆盖率变化——查询由路由模型现场生成。要比答案质量先冻结查询：`eval_answers --frozen-queries` 已加（查询列表、轮数、覆盖率逐题一致），并配套本地门禁 `scripts/quality_gate.py`。
- **判定噪声地板**：同一批答案隔两天重判，每维度 95% CI ±0.07~0.12、无据主张 ±15~31。小于这个地板的差不算变化。
- **内嵌 Qdrant 单进程**：召回/答案评测串行跑；判定不碰它。
- **重新摄入会清字段**：`persist_units`/`persist_figures` 是全列 merge，会抹掉 `figure_ids`、VLM 描述、图注链接。
- **生成解析的 `Extra data`（两个 JSON 对象拼接）已修**：网关按「严格 → 容忍控制字符 → 取第一个完整对象」解析，失败时抛带分类与原文前 120 字的错误（`feat/production-readiness`）。

## 下一步（未经批准，先别动手）

1. ~~第二波：判定器看清真证据；条件化主张渲染的键名不匹配。~~ **已在 `feat/production-readiness` 落地**（`.scratch/production-readiness/`，9 张票全 resolved）：判定器看到 ≤20 条 × 400 字带证据 id 与定位、规模写进轨迹；条件由 `_claim_conditions` 一处从 `context`/`modality` 映射，无条件不渲染条件字样。同分支还给了冻结查询口径、质量门禁与基线、降级标记、`/query` debug 块、内嵌存储独占锁与启动自检。
2. 机制题：63 个期望点只召回 20，占全部缺口的 43/73；图谱输入太稀（16,644 标题只连出 328 条边）。
