# Handoff: 图表读图（完成，评测已跑）

Spec 与票 01–08 已实现、评审、合并进 `main`。不要重开；本文件记录发货状态、首次实测数字与复核方式。

## State

- 分支：`main`（`feat/chart-analysis` 已合并，工作树与 `ticket/*` 分支已删除）。
- 票：`issues/01`…`08` 全部 `resolved`；`spec.md` 与 `00-decisions.md` 为 `resolved`。
- 测试：`E:/PAknow/.venv/Scripts/python.exe -m pytest tests -q` → **323 passed**。
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

## Verification

```bash
E:/PAknow/.venv/Scripts/python.exe -m pytest tests -q          # 323 passed
E:/PAknow/.venv/Scripts/python.exe scripts/check_scratch.py    # OK

# 单题冒烟（改完答案上限后跑过：chart-01 62.4s，ok）
PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db OBJECTS_DIR=E:/PAknow/data/objects PYTHONPATH=. \
  python scripts/eval_chart.py --limit 1 --out .scratch/chart-analysis/eval/smoke-guard
```

## Open items

- **金标准未经人工确认**：`chart_v1.json` 的 `annotation` 字段自己声明期望值取自被测模型的一次读图，必须由 owner 对照原页核对。因此 0% 漂移不能当独立证据（labels 与答案同源）。图像在 `data/market/charts/chart-*.png`（gitignore）。
- 多读 8% 里有多少是金标准漏列刻度、有多少是答案多读，只有人工核对后能分开；chart-02 出现同一价位重复（`1291–1301` 各两次、chart-09 `172/174/176` 各两次），那是同一条带内两块面板各自的价格轴，折叠前要先确认。
- 表头最新价与右侧价格轴的冲突（chart-03/04/05/08/10）一律只记 `unreadable`，没有作为价位使用；这是决议，不是缺陷。
- 跨条带趋势描述仍可能含糊（条带方案的固有代价）。
- 读图延迟 +20s、推理 token 单次 5–6k，120s 预算是硬上限，不要靠重试解决读图失败（`attempts=1` 是故意的）。
