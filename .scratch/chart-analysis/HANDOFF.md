# Handoff: 图表读图（完成，评测已跑）

Spec 与票 01–08 已实现、评审、合并进 `main`。不要重开；本文件记录发货状态、首次实测数字与复核方式。

## State

- 分支：`main`（`feat/chart-analysis` 已合并，工作树与 `ticket/*` 分支已删除）。
- 票：`issues/01`…`08` 全部 `resolved`；`spec.md` 与 `00-decisions.md` 为 `resolved`。
- 测试：`E:/PAknow/.venv/Scripts/python.exe -m pytest tests -q` → 全绿。
- `python scripts/check_scratch.py` → OK。
- Review base `2f412e6`；两轴复审结论见 `.scratch/chart-analysis/spec.md` 的 Comments。

## Shipped

- 链路 `route → read → retrieve → check → generate`；读图一次、失败可见（`chart_read_status="unstructured"` + `_CHART_FALLBACK_SYSTEM` 单阶段降级）、非行情图拒答、图表问题关图谱扩展。
- `chart_read` 出现在 `/charts` 响应、页面「读图数据」面板、`output`/`/runs/{id}`；读图永不带 `[n]`（ADR-0002）。
- `scripts/eval_chart.py` + `evals/gold/chart_v1.json`（10 题，仅确定性判定：漏读 / 多读 / 漂移）。

## 首次实测：带图 10 题全量（本目录另存）

`PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db OBJECTS_DIR=E:/PAknow/data/objects PYTHONPATH=. \
  python scripts/eval_chart.py --out .scratch/chart-analysis/eval/<ts>`（`--resume` 续跑）

| 指标 | 值 |
| --- | ---: |
| 题数 | 10/10 ok，0 skipped，0 errors（首跑 chart-10 报错，重跑通过） |
| 漏读 | 3/164 = **1.8%**（chart-06 `36.14`；chart-09 `184.0`、`171.0`） |
| 多读 | 14/175 = **8.0%**（chart-02 独占 5 条） |
| 漂移 | 0/161 = **0%** |

产物：`.scratch/chart-analysis/eval/20261024T125057Z`（`records.jsonl` 逐题、`summary.json` 汇总）。

## 本轮修掉的一个缺陷

chart-10 首跑 `JSONDecodeError: Unterminated string ... char 10`——偏移正好是 `{"answer":"`：**答案**生成的 JSON 被输出上限截断。这是图表链上唯一没有护栏的结构化调用（`_generate_node` 与 `run_query` 都不捕获），会直接冒成 `/charts` 的 500。

- `paknow/workflows/generate.py`：答案调用 `max_tokens` 8192 → 16384（推理 token 与正文共享上限；读图调用此前因同一原因从 4096 提到 8192），并捕获后返回可见降级 `_DEGRADED_ANSWER` + `generate_failure`。
- `paknow/workflows/graph.py`：`_generate_node` 把 `generate_failure` 记进 `state["failures"]`（`generate: ...`），`/runs/{id}` 可见。
- 回归测试：`tests/test_chart_answer.py` 两条（截断不冒泡、降级进 trace）。

## 口径重审（进行中）

上面那三个率衡量的是**“印刷数字抄全没有”**，不是“关键位找准没有”：金标准 164 条里 159 条是轴刻度，10 张图的 `expected_swings` 全为空，读图提示词还明令“不得按刻度或轴线推算”。

新口径（owner 已定，尚未落到产品）：

- 表头数值（`L=`、`MAX 20 (20)`）与价格轴刻度**不算**关键位；
- 图上标注只用于**定位**，关键价按相邻两条刻度插值估算，容差**半格**（`chart-09` 轴间距 2.0 → ±1.0，其余 ±0.5，见 `proto_steps.json`）；
- 形态必须给「名称 / 位置 / 依据 / 把握」，只写名称不输出。

探针 `.scratch/chart-analysis/proto_key_levels.py`（未接产品）+ 10 张读数 `proto/`：

- **读图输出上限必须 16384**：8192 下 10 张里 9 张失败（8 张空内容、2 张 JSON 截断），16384 下 10/10 成功，单张约 34s。
- 关键位、摆动点、形态全部中文；`chart-06` 此前 `basis` 退化成英文，已由语言约束修好。
- 已知未解：多面板/多品种被压进单值字段（`chart-02` 的形态引用不存在的条带 3–6，`chart-03` 的 `instrument="ESU08 / AAPL"`、`market_state=unclear`）；少量形态文本缺冒号。

因此 `## 首次实测` 的 1.8% / 8.0% / 0 属于**旧口径**，不与新口径交叉比较。

## Verification

```bash
E:/PAknow/.venv/Scripts/python.exe -m pytest tests -q          # 全部通过
E:/PAknow/.venv/Scripts/python.exe scripts/check_scratch.py    # OK

# 单题冒烟（改完答案上限后跑过：chart-01 62.4s，ok）
PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db OBJECTS_DIR=E:/PAknow/data/objects PYTHONPATH=. \
  python scripts/eval_chart.py --limit 1 --out .scratch/chart-analysis/eval/<ts>   # 冒烟跑完即删，不入库
```

## Open items

- **金标准未经人工确认**：`chart_v1.json` 的 `annotation` 字段自己声明期望值取自被测模型的一次读图，必须由 owner 对照原页核对。因此 0% 漂移不能当独立证据（labels 与答案同源）。图像在 `data/market/charts/chart-*.png`（gitignore）。
- 多读 8% 里有多少是金标准漏列刻度、有多少是答案多读，只有人工核对后能分开；chart-02 出现同一价位重复（`1291–1301` 各两次、chart-09 `172/174/176` 各两次），那是同一条带内两块面板各自的价格轴，折叠前要先确认。
- 表头最新价与右侧价格轴的冲突（chart-03/04/05/08/10）一律只记 `unreadable`，没有作为价位使用；这是决议，不是缺陷。
- 跨条带趋势描述仍可能含糊（条带方案的固有代价）。
- 读图延迟 +20s、推理 token 单次 5–6k，120s 预算是硬上限，不要靠重试解决读图失败（`attempts=1` 是故意的）。
