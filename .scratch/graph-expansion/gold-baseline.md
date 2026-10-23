# 金标准基线（01 号票）

- 数据集：`evals/gold/graph_expansion.json`（20 题：百科 10、机制 10）
- 协议：hybrid_search(top_k=20) per frozen query; top-20 union; no routing, no rerank
- 运行命令：`python scripts/eval_gold.py`
- 结果文件：`.scratch/graph-expansion/gold-baseline.json`

## 汇总

| 指标 | 数值 |
| --- | --- |
| 期望证据总数 | 50 |
| 文本 top-20 并集命中 | 24 |
| 未命中（图谱独有候选） | 26 |
| 完全命中的题 | 10/20 |
| 至少一条期望证据在图外 | 10/20 |

## 逐题

| 题号 | 类别 | 命中/期望 | 未命中证据 |
| --- | --- | --- | --- |
| enc-01 | encyclopedia | 1/1 | — |
| enc-02 | encyclopedia | 2/3 | 7e71a9e4 |
| enc-03 | encyclopedia | 1/1 | — |
| enc-04 | encyclopedia | 3/3 | — |
| enc-05 | encyclopedia | 3/3 | — |
| enc-06 | encyclopedia | 2/2 | — |
| enc-07 | encyclopedia | 2/2 | — |
| enc-08 | encyclopedia | 1/1 | — |
| enc-09 | encyclopedia | 2/2 | — |
| enc-10 | encyclopedia | 1/1 | — |
| mech-01 | mechanism | 1/3 | 38de0072、cef8b481 |
| mech-02 | mechanism | 0/4 | e7556931、6bf656e6、c60dded0、89392667 |
| mech-03 | mechanism | 2/4 | b05f7402、1b3c4a17 |
| mech-04 | mechanism | 0/4 | 9117c447、7647d8b1、1c216bfd、dded563f |
| mech-05 | mechanism | 0/4 | dad1f6d5、a22445b3、ad5df18d、e7a6eb6e |
| mech-06 | mechanism | 1/2 | 3b07a477 |
| mech-07 | mechanism | 1/3 | 964597e4、53d1ed97 |
| mech-08 | mechanism | 1/1 | — |
| mech-09 | mechanism | 0/3 | 8ef770a0、9fa2638a、964597e4 |
| mech-10 | mechanism | 0/3 | 54c4bd4a、4c652ffa、3b08ac8b |

## 口径

每题 6 条扩展查询在标注时冻结；每查询取 `hybrid_search(top_k=20)`，取并集。未命中至少一条期望证据即记为「图独有」候选。基线不调用路由模型。
