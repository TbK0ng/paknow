# Handoff: 评测口径与答案级评测（完成）

Spec 与票 01–05 已实现、评审、合并。不要重开；本文件记录发货状态与复核方式。

## State

- 分支：已合并进 `main`（`8944bfb` 是 `main` 的祖先）；`feat/*` 分支已删除。
- 票：`issues/01`…`05` 全部 resolved；spec `spec.md` 为 resolved。
- 测试：`E:/PAknow/.venv/Scripts/python.exe -m pytest tests -q` → **323 passed**。
- Review：两轴结论与修复见 `.scratch/retrieval-eval-parity/spec.md` 的 Comments；修复提交 `68d0bcc`，合并 `8944bfb`。
- 工作树 `E:/PAknow-wt/*` 与 `ticket/*` 分支已在收尾时删除。

## Shipped

### 召回评测（`scripts/eval_gold.py`）

- `--mode baseline`：并集探针，行为与产物不变、不含 `text_width`。
- `--mode text`：生产等价基线，同一检索缝、`expand_graph=False`。
- `--mode graph`：图谱回归；同时报告相对并集探针与相对生产等价基线的差值。
- 指标：`graph_arm_found`（图谱臂文本 ∪ 图谱命中）、`graph_only`、`net_gain = graph_arm_found − regression_text_found`。
- `MAX_EVIDENCE=14`；`MAX_CONTEXT_TOKENS=1200`、`MAX_EDGES=32` 不变。

### 题库 v2（`evals/gold/graph_expansion_v2.json`）

- 60 题：百科 20、机制 20、例外/失效 10、带条件主张 10。
- 原 20 题的四个冻结字段逐字段不变；全部 60 题都有 2–5 条 `expected_points`（绑定来源证据）。
- 哈希 sidecar：`7cf35d94…d55560`；`.gitattributes` 固定 `-text`，跨平台字节稳定。
- 独立核对记录：`dataset-residuals.json`（发布集残差 0）。

### 答案级评测

- `scripts/eval_answers.py`：每条题在独立上下文跑 `run_query`，两条臂（图谱开/关），逐题落盘答案、引用 id、可引用集合、路由、轮数、失败；确定性硬约束 + 覆盖率；`--limit`/`--resume`。
- `scripts/eval_judge.py`：盲判（输入只有题面、期望要点、答案、引用证据文本），三段 0–2 分 + 不支持断言；硬约束失败直接判负；`--sample`/`--resume`。
- 公共 helper 在 `scripts/eval_common.py`（digest、display path、JSONL checkpoint）。

## 结果（可复核）

召回 v1（20 题，50 条期望）：文本 16，图谱臂并集 28，**净增益 +12**；图谱独有 12；失败 0。

宽度消融 v2（60 题，123 条期望）：

| 运行 | 命中 | 净增益 |
|---|---:|---:|
| 并集探针 | 73 | — |
| text w14 / w20 | 41 / 42 | — |
| graph w14 / w20 | 55 / 55 | +14 / +13 |

生产宽度保持 **14**；宽 20 只多召回到 1 条已被图谱覆盖的证据。

答案与判定（120 条，硬约束 100%）：

| 臂 | 正确性 | 条件保留 | 引用支撑 | 不支持断言 | 覆盖率 |
|---|---:|---:|---:|---:|---:|
| text | 1.183 | 1.300 | 1.300 | 146 | 0.233 |
| graph | 1.250 | 1.233 | 1.250 | 119 | 0.250 |

配对差：+0.067 / −0.067 / −0.050（better 10 / worse 6 / equal 44）。图谱在召回层有增量，在答案层是混合结果。

## Verification

```bash
# tests
E:/PAknow/.venv/Scripts/python.exe -m pytest tests -q

# recall (serial; embedded Qdrant single process)
PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db OBJECTS_DIR=E:/PAknow/data/objects PYTHONPATH=. \
  python scripts/eval_gold.py --mode text --dataset evals/gold/graph_expansion_v2.json
PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db OBJECTS_DIR=E:/PAknow/data/objects PYTHONPATH=. \
  python scripts/eval_gold.py --mode graph --dataset evals/gold/graph_expansion_v2.json \
  --reuse-baseline --baseline .scratch/retrieval-eval-parity/gold-v2-baseline.json

# answers (real model calls) then judge
PYTHONPATH=. python scripts/eval_answers.py --resume --out .scratch/retrieval-eval-parity/answers/<ts>
PYTHONPATH=. python scripts/eval_judge.py --resume --out .scratch/retrieval-eval-parity/judgements/<ts>
```

## Open items

- **生产召回宽度**仍是 14；若要改成 20，必须单独开变更并评估提示词增长（本 spec 明确排除）。
- 判定分数在 temperature 0 下重跑仍会漂移，只作诊断，不能当门禁；若要做门禁，先解决判定稳定性。
- 回答的「引用越界 / 引用图谱 id」两项硬约束在当前 `generate()` 实现下结构上难以触发；保留为不变量护栏。
- PR #1（`feat/graph-expansion`）与本分支都已合并进 `main`；合并顺序的约束已解除。
- tracker 没有 `blocked` 状态：阻塞票只能用 `needs-triage` 暂存，前置完成后手动翻 `ready-for-agent`（`check_scratch.py` 的约束）。
