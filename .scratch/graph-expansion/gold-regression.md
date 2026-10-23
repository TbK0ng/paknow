# 金标准回归（05 号票）

- 数据集：`evals/gold/graph_expansion.json`（20 题：百科 10、机制 10；每题 6 条冻结扩展查询；本票未改动，逐字节与 01 号票一致）
- 基线：`.scratch/graph-expansion/gold-baseline.json`（`--mode baseline`，每查询 `hybrid_search(top_k=20)` 取并集）
- 回归：`.scratch/graph-expansion/gold-regression.json`（`--mode graph`，同一批冻结查询走 `retrieve_evidence`）
- 复现命令（在仓库根目录，先跑基线再跑回归；embedded Qdrant 单进程，不可并发）：

```bash
PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db OBJECTS_DIR=E:/PAknow/data/objects PYTHONPATH=. \
  python scripts/eval_gold.py --mode baseline

PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db OBJECTS_DIR=E:/PAknow/data/objects PYTHONPATH=. \
  python scripts/eval_gold.py --mode graph
```

基线本次重跑与 01 号票记录逐字段一致（24/50、10/20、10/20），数据集未被重新生成。

## 口径（沿用 01 号票，不调整）

- **图独有**：该题至少一条期望证据不在 6 条扩展查询的 top-20 并集内。
- **新增召回**：图谱证据中的期望证据，且不在基线并集、也不在新链路文本命中内（可归因于图谱扩展）。
- **丢失**：基线并集命中、但新链路文本与图谱都没有的期望证据。另列「新链路文本单独丢失」以区分原因。
- 回归走线上检索缝 `retrieve_evidence`：文本侧 6 查询 top-40 → 重排 top-20 → 多样性取 14。

## 并列汇总

| 指标 | 基线（文本 top-20 并集） | 回归（新链路 + 图谱扩展） | 差 |
| --- | ---: | ---: | ---: |
| 期望证据总数 | 50 | 50 | — |
| 期望证据命中（文本 ∪ 图谱） | 24 | 24 | 0 |
| 其中：新链路文本命中 | — | 16 | −8 |
| 其中：图谱命中期望证据 | — | 9 | +9 |
| 图谱可归因新增 | — | 6 | +6 |
| 丢失基线证据 | — | 6 | −6 |
| 其中：新链路文本单独丢失 | — | 8 | — |
| 仍未命中 | 26 | 20 | −6 |
| 完全命中题 | 10/20 | 7/20 | −3 |
| 图独有题 | 10/20 | 10/20 | — |
| 图独有题被本次覆盖 | — | 5/10 | — |
| 图谱条目总数 | — | 160（每题 8） | — |
| 触发预算截断的题 | — | 20（均为 `truncated_evidence`，边/token 未触顶） | — |
| 检索失败 | — | 0 | — |

## 逐题

| 题号 | 类别 | 期望 | 基线命中 | 新链路文本 | 图谱新增 | 丢失 | 回归合计命中 | 图独有 | 图独有被覆盖 | 截断 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |
| enc-01 | encyclopedia | 1 | 1 | 0 | 0 | 1 | 0 | 否 | 否 | evidence |
| enc-02 | encyclopedia | 3 | 2 | 2 | 0 | 0 | 2 | 是 | 否 | evidence |
| enc-03 | encyclopedia | 1 | 1 | 1 | 0 | 0 | 1 | 否 | 否 | evidence |
| enc-04 | encyclopedia | 3 | 3 | 2 | 0 | 1 | 2 | 否 | 否 | evidence |
| enc-05 | encyclopedia | 3 | 3 | 2 | 0 | 1 | 2 | 否 | 否 | evidence |
| enc-06 | encyclopedia | 2 | 2 | 1 | 0 | 1 | 1 | 否 | 否 | evidence |
| enc-07 | encyclopedia | 2 | 2 | 2 | 0 | 0 | 2 | 否 | 否 | evidence |
| enc-08 | encyclopedia | 1 | 1 | 1 | 0 | 0 | 1 | 否 | 否 | evidence |
| enc-09 | encyclopedia | 2 | 2 | 1 | 0 | 1 | 1 | 否 | 否 | evidence |
| enc-10 | encyclopedia | 1 | 1 | 1 | 0 | 0 | 1 | 否 | 否 | evidence |
| mech-01 | mechanism | 3 | 1 | 0 | 2 | 0 | 3 | 是 | 是 | evidence |
| mech-02 | mechanism | 4 | 0 | 0 | 0 | 0 | 0 | 是 | 否 | evidence |
| mech-03 | mechanism | 4 | 2 | 1 | 0 | 1 | 1 | 是 | 否 | evidence |
| mech-04 | mechanism | 4 | 0 | 0 | 1 | 0 | 1 | 是 | 是 | evidence |
| mech-05 | mechanism | 4 | 0 | 0 | 0 | 0 | 0 | 是 | 否 | evidence |
| mech-06 | mechanism | 2 | 1 | 0 | 1 | 0 | 2 | 是 | 是 | evidence |
| mech-07 | mechanism | 3 | 1 | 1 | 1 | 0 | 2 | 是 | 是 | evidence |
| mech-08 | mechanism | 1 | 1 | 1 | 0 | 0 | 1 | 否 | 否 | evidence |
| mech-09 | mechanism | 3 | 0 | 0 | 1 | 0 | 1 | 是 | 是 | evidence |
| mech-10 | mechanism | 3 | 0 | 0 | 0 | 0 | 0 | 是 | 否 | evidence |

## 结论（不迁就结果）

**支持的部分**：按固定的「图独有」口径（top-20 并集之外），图谱扩展在 5 道题上召回了 6 条期望证据，这些证据在新链路文本命中里也不存在。就图谱这一机制而言，它确实能拿到文本通道拿不到的证据。

**证伪的部分**：相对基线，回归没有净增益（净增 0）：命中数 24 → 24，仍未命中 26 → 20。原因不在图谱：新链路文本侧被重排与多样性截到 14 条，基线并集里的 8 条期望证据因此掉出；图谱补回其中 2 条，净丢失 6 条。图谱新增 6 条与文本丢失 6 条正好抵消。

因此：

- 「图谱扩展能召回文本 top-20 并集外的证据」——**成立（6 条 / 5 题）**。
- 「图谱扩展带来净召回增益」——**不成立（净增 0）**，且新链路相对基线出现 6 条基线证据的净回归；按票面要求记录证伪，不调整口径。

**预算观察**：20 道题全部触到「回取证据 ≤8」上限（`truncated_evidence`），160 条图谱条目全部被选中即停；边 ≤32 与图块 ≤1,200 tokens 均未触顶。也就是说当前约束召回量的是证据条数，不是 token 预算；若要扩大扩展，先评估「证据 ≤8」是否过紧，而不是放宽 token。

**副作用说明**：本次回归同时暴露了文本侧宽度问题（top-40 → 重排 20 → 取 14）。这与图谱扩展无关，但会淹没图谱的增量；后续若要单独衡量图谱，应保持文本侧协议不变，或把文本宽度作为独立变量消融。
