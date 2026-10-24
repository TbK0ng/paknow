---
description: "图资产提取、VLM 观察（派生材料）、视觉路线替换、观察污染事故、条件化主张、BarRef 与图文链接。"
---

# 2. 多模态证据构建

> 这批资料的核心难点是图表。本章讲：图资产怎么管、视觉路线为什么被迫换方案、图谱与 K 线指代怎么构建、图文链接踩了什么坑。

> **计数口径**：本章出现的规模类数字（图 927、单元 91,235、图谱 1,105 节点 / 3,753 边、`bar_refs` 674 等）都是**本地库快照 2026-10-24**（`data/paknow.db`，不入 git）的数；历史叙述里的数（如污染 725 条）标注了"当时"。

## 1. 图资产（FigureAsset）

927 张图，每张记录：

| 字段 | 用途 |
|---|---|
| `figure_id` + `rel_path`（内容寻址） | 去重、可重复引用同一张图 |
| `locator.bbox` / `pdf_page_1based` | 页内定位、图文几何绑定 |
| `figure_label` | 图号（如 4.4 / FIGURE I.1），用于"图N"检索 |
| `caption_ids` / `explanation_ids` | 图 ↔ 图注/解释段落双向链接 |
| `vlm_description` + `description_model` | 视觉观察（派生材料） |

### 1.1 一个必须记录的坑：几何信息在 `locator.bbox`，不在 `page_bbox` 列

`page_bbox` 列在 JSON 中大量为 `null`，但 SQL 的 `IS NOT NULL` 判断会把它算作"非空"，导致统计（927 张"有 bbox"）与样本（bbox=None）矛盾。真实几何在 `locator["bbox"]`。**教训：JSON 列的 null 与 SQL NULL 不是一回事，统计脚本必须看值而不是看列。**

## 2. 视觉路线：为什么不是 ColPali

原计划用 ColPali/ColQwen2 做页面级视觉检索（late-interaction 多向量）。实测全部不可用：

| 方案 | 结果 | 原因 |
|---|---|---|
| ColPali（PaliGemma） | ❌ CUDA 崩溃 | Blackwell sm_120 + torch 2.9.1 触发 `vectorized gather kernel index out of bounds` |
| ColQwen2 | ❌ 依赖冲突 | checkpoint 需要 `colpali_engine`（要求 transformers 4.x），项目锁 5.x |
| 云端 vision 全量索引 927 图 | ❌ 成本/延迟 | 一次性索引可接受，但每页实时分析不可接受，且离线不可用 |

**采用的替代：Qwen2.5-VL-3B 观察式**

- 本地、transformers 5.x 原生、Blackwell 可用；
- 对每张图输出**结构化观察**：`可见事实 / 图中文字 / 不确定处`；
- 观察文本进入同一 dense+sparse 索引，成为"派生索引材料"。

**为什么不假装这是图像检索**：观察式检索是"用文本代理图像"，能回答"哪张图与这个描述相关"，但**不能**做 image-to-image 相似度。文档里必须如实标注这个能力边界。

## 3. 观察文本的定位：派生材料，不是原文

这是第 2 章最重要的设计。927 条观察单元：

- `block_type=caption`，但 `provenance=derived_observation`；
- 生成时**移出可引用证据列表**，进入独立"图读辅助"块（无 `[n]` 编号、明确标注不可引用、最多 6 条）；
- 提示词禁止模型引用或罗列"图 X 具备该特征"。

### 3.1 一次 725 条数据污染事故

第一批视觉观察在存储时把 **chat template 的输入和输出一起**写进了单元文本（`user\n<prompt>\nassistant\n<输出>`）。后果：

- 观察单元的文本包含提示词本身（可被检索到）；
- 观察文本长度膨胀，稀疏/稠密向量都失真；
- 更严重的是：它被当作"原文证据"参与回答。

修复（`scripts/fix_observations.py` 重写）：

1. 以最后一个 `assistant` 标记为界剥离前缀（Qwen 模板里 `<|im_start|>` 被 `skip_special_tokens` 跳过，但 `assistant` 作为普通 token 保留）；
2. 重写 `text_hash`，重新做 dense+sparse 嵌入；
3. 全部观察单元标记 `derived_observation`。

实测结果（当时）：污染 725 → 0，派生标记 927。现在按文本里含 `assistant` 模板残留在库里数，是 0 条。

> 旧版脚本为什么没修好？它在 `with SessionLocal()` 之外调用 `commit()`，session 已关闭，清洗从未落库。这是"脚本跑完没有报错但什么也没发生"的典型。

## 4. 证据图谱

### 4.1 结构与规模

| 元素 | 数量 | 说明 |
|---|---|---|
| 节点总数 | 1,105 | 178 concept + 927 figure |
| 概念节点 | 178 | 从标题/图注抽取 |
| 图节点 | 927 | 与 `ILLUSTRATED_BY` 边相连 |
| 主张（claim） | 1,888 | subject + predicate + **qualifiers** |
| 边总数 | 3,753 | 见下表 |

边按关系拆开（本地库快照 2026-10-24）：

| 关系 | 数量 | 来源 |
|---|---|---|
| `SUPPORTED_BY` | 1,888 | claim_extract |
| `ILLUSTRATED_BY` | 1,529 | figure_extract |
| `INVALIDATED_BY` | 181 | heading_parse |
| `TRANSITIONS_TO` | 146 | heading_parse 142 + seed 4 |
| `PART_OF` | 7 | heading_parse 3 + seed 4 |
| `EXCEPTION_TO` | 2 | heading_parse |

**口径说明**：概念节点只有 178 个，不是几千——图节点反而占了 84%。这张图是"图目录 + 少量概念与主张"，规模在拍数时以 edge 为主，别拿节点数当知识量。

### 4.2 为什么主张必须带条件

```json
{"subject": "高二", "predicate": "是可靠的入场信号",
 "qualifiers": {"context": ["强趋势"], "modality": "usually"}}
```

**为什么**：如果丢掉 `context`，图谱会得出"高二总是可靠"这种错误结论。所以 `qualifiers` 原样随主张进入生成上下文。

**但要如实说清实现口径**：扩展代码读的是 `conditions` / `exceptions` 两个键（`paknow/graph/expansion.py:277-278`），而库里 1,888 条主张的 qualifiers 只有 `context` / `modality`——这两个键一条都没有。结论：条件现在是**透传**，不是**筛选**。主张进入上下文的真实顺序是 **种子概念顺序 → `extraction_confidence` 降序 → `claim_id`**（`paknow/graph/expansion.py:238-243`）；文档里曾经写的"主题匹配 > 谓词匹配 > 条件匹配"是策划稿，代码里不存在。

### 4.3 趋势生命周期：一个小而关键的种子

`TRANSITIONS_TO` 共 146 条边（heading_parse 142 + seed 4），而有意思的是 seed 那 4 条，它串起了一个环：

```mermaid
flowchart LR
    S[脉冲 Spike] --> T[紧密通道 Tight Channel]
    T --> B[宽松通道 Broad Channel]
    B --> R[交易区间 Trading Range]
    R --> S
```

**为什么值得单独建**：框架类问题（"市场周期如何转换"）的关键答案就是这个环。纯文本检索能召回描述，但生成器很难自己拼出"可循环"的结构；图谱提供确定的结构骨架。

### 4.4 概念别名表曾经是空的

`concept_aliases` 表设计用于双语概念对齐，但图谱抽取从未写入（0 行）。恢复方案：

- 图检索不依赖该表，而是用 `terms.yaml` 词典做**桥接**：查询 → canonical → 中英别名 → 匹配概念节点标签；
- 词典本身：`configs/terms.yaml` 有 **236 个条目**，其中 **233 个不同的 canonical**——`load_terms()` 会把重复 canonical 的块合并成一条（当前重复的是 `auto_room_veto`、`auto_undershoot`、`broad_channel` 三条，`paknow/retrieval/dictionary.py:36-42`），所以"233"是合并后的概念数，不是文件行数；
- 别名表本身：`concept_aliases` 当前 **2,703 行 `canonical_concept`、3,019 条别名**（本地库快照 2026-10-24），全部 `review_status=pending`；其中 178 个 `auto_*` 来自标题语法抽取。它和 `terms.yaml` 的 233 个词典概念不是同一口径：词典是检索桥，别名表是图谱侧的对齐记录，别互相当计数用。概念身份的唯一来源仍是 `configs/terms.yaml`（经 `paknow/graph/identity.py` 解析），别名表只是它的下游快照。

## 5. BarRef：K 线指代结构化

### 5.1 问题

书中大量指代是"图中 K 线 2 与 3 构成双底"。普通文本检索能召回这段文字，但**无法回答**"K 线 2 和 3 构成什么形态"——因为编号是叙述性文本，没有被结构化。

### 5.2 设计

新表 `bar_refs`：`figure_id / evidence_id / bars / pattern / relation / snippet`。

- 提取：对图注 + 图文关联单元做批量 LLM 提取（6 条/批，2400 tokens，失败重试一次）；
- 幂等：`ref_id = sha1(f"{doc_id}:{evidence_id}:{bars_key}:{pattern}").hexdigest()[:16]`，即 sha1 截断成 **16 个十六进制字符**（`scripts/extract_barrefs.py:112`），重跑落到同一个 `ref_id` 上（`merge` 覆盖），不会产生重复行；
- 检索：`barref_lookup` 解析查询里的 `K线N`/`图N`，按编号交集 + 图号匹配；
- 结果：`bar_refs` 现有 674 条（本地库快照 2026-10-24）。"覆盖 424/591 个候选单元"是当时提取跑批的统计口径，仓库里没有留下日志或复核脚本，**这两个数无法从当前数据复现**；能确认的只有 674 这个终点值。

### 5.3 为什么不做成"图像识别"

理论上可以用视觉模型直接读图上的 K 线编号。但：

- 图中编号是印刷体，识别可行，但**编号与形态的语义关系**仍要语言模型判断；
- 全量跑视觉识别（927 图 × 多次调用）成本高；
- 图注/解释段落已经用文字描述了编号关系，文本提取的性价比更高。

## 6. 图文双向链接：从 0 到 3,723

### 6.1 发现

`FigureAssetRow.caption_ids/explanation_ids` 全部为空；PDF 图注单元的 `figure_ids` 也为空（修复前只有 1,528 条单元带链接，主要来自 EPUB 与观察单元）。结果是"给我看图例"时，生成器拿不到任何真实 figure_id，只能编造"图4.4"这样的字符串。

### 6.2 修复

两条策略（`scripts/link_captions_to_figures.py`）：

1. **几何**：同页 + x 轴重叠 + 图注在图的 y 范围上下 120pt 内 → 链接（2,244 条）。注意：图注可能在图**上方**（y 更小），只判"下方"会全部漏掉。
2. **标签**：图注 "图4.4" → 图号 `4.4` 匹配（80 条）；并从图注回填 PDF 图的 `figure_label`（276 个）。

同时同步 Qdrant payload 的 `figure_ids`（否则 caption 召回到的仍是空链接）。最终：单元侧 1,528 → 3,723；图侧 `caption_ids` 927 张、`explanation_ids` 907 张。

**这四个数不是回忆，是有日志的**：`scripts/link_captions_to_figures.py` 跑完会打印 `geo=… label=… labels_set=… payload=…`，`data/link_log.txt:3` 记着一行 `geo=2244 label=80 labels_set=276 payload=2195`——即 2,244 条几何候选、80 条标签命中、276 个回填的图号、2,195 个同步了 payload 的单元。单元侧的 3,723 与图侧的 927/907 是同一快照（本地库快照 2026-10-24）里数出来的。要复核就重跑脚本对日志，而不是翻文档。

## 7. 本章小结

- 视觉路线是"被迫的合理选择"：能力有边界，文档如实标注。
- 派生材料的定位必须贯穿到生成提示，否则引用体系会被污染。
- 图谱的价值不在规模，而在**条件**与**环**这两个结构。
- 图文链接是"看起来有、实际没有"的高危区，必须用计数验证。
