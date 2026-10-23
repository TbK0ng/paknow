# 召回宽度消融（02 号票）

- 数据集：`evals/gold/graph_expansion_v2.json`，60 题（百科 20、机制 20、例外/失效 10、带条件主张 10），每题 6 条冻结扩展查询
- 数据集 SHA-256：`f635cb2d6ecfb395d645ab393e6cda7108c07df1cb319ac992e667742d28f4ba`，与 `evals/gold/graph_expansion_v2.sha256` 一致；本次未改动数据集
- 检索缝：`retrieve_evidence(..., text_width=N)` → `diversify(top_k=N, max_per_doc=5)`；默认 14 与线上一致
- 基线：`gold-v2-baseline.json`（并集探针，每查询 `hybrid_search(top_k=20)` 后取并集；两次 graph 运行的 `--reuse-baseline` 均先校验上面的 SHA-256）
- 运行方式：四次运行串行完成（embedded Qdrant 单进程，不可并发），全部产物在 `.scratch/retrieval-eval-parity/`

## 结果

| 运行 | 模式 | 宽度 | 题数 | 期望命中（并集 / 总期望） | 其中文本臂 | 其中图谱证据 | 净增益 | 截断 | 失败 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| `gold-v2-baseline.json` | baseline（并集探针） | 14* | 60 | 73 / 123 | — | — | — | — | 0 |
| `gold-v2-text-w14.json` | text | 14 | 60 | 41 / 123 | 41 | — | — | — | 0 |
| `gold-v2-text-w20.json` | text | 20 | 60 | 42 / 123 | 42 | — | — | — | 0 |
| `gold-v2-graph-w14.json` | graph | 14 | 60 | 55 / 123 | 41 | 18 | +14 | 60 题 `evidence` | 0 |
| `gold-v2-graph-w20.json` | graph | 20 | 60 | 55 / 123 | 42 | 18 | +13 | 60 题 `evidence` | 0 |

\* baseline 无多样性步骤；宽度只被接受并记录在产物顶层，不影响并集探针的数字。

补充口径：

- 净增益 = 图谱臂并集命中 − 同宽度文本臂命中（`union_found − regression_text_found`）。
- 完全命中题：baseline 39/60；text 14 与 20 均为 20/60。
- 图独有题（期望证据落在 top-20 并集之外）：21/60，本次四次运行不变。
- graph 运行相对并集探针：图谱证据命中 18 条，新召回 9 条，丢失 27 条（其中文本路径单独丢失 32 / 31 条）；图谱条目 840 条（每题 14），全部 60 题都先触发「证据条数 ≤14」上限，边 ≤32 与 token ≤1,200 均未触顶。
- 所有运行的 `failures` 均为空。终端退出时出现的 `QdrantClient.__del__` 警告是客户端关闭顺序问题，不写入产物，也不是检索失败。

## 宽度 × 图谱交互判断

- **文本臂 14 → 20：只多 1 条期望证据。** 逐题对比仅 `mech-06` 的期望命中从 0 变为 1（多出 `0bcafd210d34306f5fd0fcd15e0a195c`）；完全命中题仍是 20/60。宽度 20 把每题的文本命中从 14 条放宽到最多 20 条，但多出来的候选在其余 59 题上没有转化为期望证据。
- **图谱证据与图谱臂并集完全不变。** 两次 graph 运行的图谱证据命中都是 18 条、条目都是 840 条、期望并集命中都是 55/123。宽度不改变图谱层本身的选择结果。
- **唯一的交互是指标归属，不是总召回。** 宽度 20 下文本臂自己拿到了 `0bcafd21…`，而这条证据在图谱臂里本来就有：于是 graph-only 从 14 降到 13、净增益从 +14 降到 +13，正好抵消文本臂多出的 1 条，总命中维持 55。也就是说，宽度与图谱在召回总量上没有协同或干扰；加宽只是让图谱的边际贡献看起来更小。
- **次生差异。** `mech-06`、`mech-17` 的图谱块 token 数在两种宽度下不同（907→863、791→718），因为更宽的文本命中会作为扩展候选的种子；两次都是选中 14 条、期望覆盖不变。这说明宽度只通过文本命中间接影响图谱的候选池，不改变图谱的命中集合。
- 结论：2×2 矩阵中，`text/20` 与 `graph/20` 相比 14 没有带来任何额外的期望证据命中；`graph/14` 是唯一在 55 条总命中下仍保留 14 条 graph-only 的组合，也是与线上同口径的组合。

## 生产宽度是否变更

**不变，生产默认仍为 14。** 宽度 20 只多召回 1 条期望证据，且这条证据在 14 宽度的图谱模式下已经被图谱臂覆盖；它却把每题文本证据从 14 条放宽到最多 20 条，增加提示词开销。图谱证据预算（14 条 / 1,200 token）、主指标 graph-only 与本次结论都以 14 为口径。若未来仍要调整生产召回宽度，需另开生产变更票，单独评估提示词增长与端到端答案质量，不在本票范围内。

## 复现

```bash
# 基线（并集探针，只在有意重录数据/口径时重跑）
PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db OBJECTS_DIR=E:/PAknow/data/objects PYTHONPATH=. \
  E:/PAknow/.venv/Scripts/python.exe scripts/eval_gold.py --mode baseline \
  --dataset evals/gold/graph_expansion_v2.json \
  --out .scratch/retrieval-eval-parity/gold-v2-baseline.json

# 文本臂
PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db OBJECTS_DIR=E:/PAknow/data/objects PYTHONPATH=. \
  E:/PAknow/.venv/Scripts/python.exe scripts/eval_gold.py --mode text \
  --dataset evals/gold/graph_expansion_v2.json --text-width 14 \
  --out .scratch/retrieval-eval-parity/gold-v2-text-w14.json
PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db OBJECTS_DIR=E:/PAknow/data/objects PYTHONPATH=. \
  E:/PAknow/.venv/Scripts/python.exe scripts/eval_gold.py --mode text \
  --dataset evals/gold/graph_expansion_v2.json --text-width 20 \
  --out .scratch/retrieval-eval-parity/gold-v2-text-w20.json

# 图谱臂（复用已校验基线）
PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db OBJECTS_DIR=E:/PAknow/data/objects PYTHONPATH=. \
  E:/PAknow/.venv/Scripts/python.exe scripts/eval_gold.py --mode graph \
  --dataset evals/gold/graph_expansion_v2.json \
  --dataset-hash evals/gold/graph_expansion_v2.sha256 \
  --baseline .scratch/retrieval-eval-parity/gold-v2-baseline.json --reuse-baseline \
  --text-width 14 --out .scratch/retrieval-eval-parity/gold-v2-graph-w14.json
PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db OBJECTS_DIR=E:/PAknow/data/objects PYTHONPATH=. \
  E:/PAknow/.venv/Scripts/python.exe scripts/eval_gold.py --mode graph \
  --dataset evals/gold/graph_expansion_v2.json \
  --dataset-hash evals/gold/graph_expansion_v2.sha256 \
  --baseline .scratch/retrieval-eval-parity/gold-v2-baseline.json --reuse-baseline \
  --text-width 20 --out .scratch/retrieval-eval-parity/gold-v2-graph-w20.json
```

- 数据与哈希不变；`--reuse-baseline` 校验的就是上面记录的 SHA-256。
- 可复现性抽查：`--mode text --text-width 14` 完整重跑一次，产物与 `gold-v2-text-w14.json` 逐字节一致（SHA-256 `fdb8eba6970ffbae8f3c8f336d1b7c7c6c4c1e8376abbb32ec3bd019f6c8ea38`）。
- 四次运行必须串行；embedded Qdrant 并发会互相锁。
