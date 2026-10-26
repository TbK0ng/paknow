---
description: "PAknow：可追溯出处的多模态 RAG、条件化证据图谱与可版本化 SOP 记忆。面向 7 本中文价格行为资料，按 RAG 设计流程讲技术选型与实测结果。"
---

<div class="paknow-hero">
  <h1>PAknow</h1>
  <p class="paknow-sub">
    面向本地价格行为资料的多模态 RAG：<strong>可追溯出处</strong>的知识问答、
    带条件的主张图谱、派生视觉索引，以及<strong>可验证、可版本化、可回滚</strong>的程序性记忆（SOP）。
  </p>
  <div class="paknow-badges">
    <span class="paknow-badge">91,978 证据单元</span>
    <span class="paknow-badge">927 图资产</span>
    <span class="paknow-badge">1,888 主张 · 3,753 边</span>
    <span class="paknow-badge">674 K 线指代</span>
    <span class="paknow-badge">3 个冻结评测集</span>
  </div>
  <div class="paknow-cta">
    <a class="paknow-btn" href="chapter0-architecture/">开始阅读</a>
    <a class="paknow-btn-ghost" href="chapter14-evaluation/">看评测数字</a>
    <a class="paknow-btn-ghost" href="https://github.com/TbK0ng/paknow">GitHub 仓库</a>
  </div>
</div>

## 三十秒版

**它解决什么**：7 本图文混排的中文交易书，回答必须能落到"书 → 章 → 图 → 页"，而不是给一段听起来对的总结。

**三个硬约束**（每条都有代码与 ADR 支撑）：

- **出处可验证**：物理页、印刷页、章节路径、图号、bbox 分开存储，绝不互相推导（[ADR-0001](https://github.com/TbK0ng/paknow/blob/main/docs/adr/0001-separate-physical-and-printed-pages.md)）。
- **派生材料不能冒充原文**：视觉观察、图谱主张、读图结果都可以影响理解，但进不了 `[n]` 引用集合（[ADR-0002](https://github.com/TbK0ng/paknow/blob/main/docs/adr/0002-derived-observations-are-not-citable-evidence.md) / [ADR-0003](https://github.com/TbK0ng/paknow/blob/main/docs/adr/0003-graph-is-an-expansion-layer-not-a-scored-route.md)）。
- **记忆可回滚**：SOP 以 YAML + git 管理，版本指纹不匹配就不可被选中。

**现在证明到哪一步**（都是可复现的数字，不吹）：

| 能力 | 结论 |
|---|---|
| 检索 | 图谱扩展同宽度下把期望证据命中从 50/123 提到 63/123（净 +13，21 题图独有）；全部 60 题撞到 ≤14 条截断 |
| 答案 | 120 次真实问答硬约束 0 失败；配对盲评 10 好 / 6 差 / 44 平 |
| 分层 | 百科与条件题覆盖尚可；**20 道机制题覆盖率 ≈0**——沿边扩展补不了论证链 |
| 读图 | 10 题全量：价位漏读 1.8%（3/164）、多读 8.0%、漂移 0；金标准是一次模型初读，尚未人工确认 |

细节与口径：[评测](chapter14-evaluation.md)。

## 设计流程

按 RAG 系统的构建顺序展开。每一章回答：这个环节要解决什么、为什么选这个方案、为什么不选另一个。

<div class="grid cards" markdown>

-   :material-sitemap-outline:{ .lg .middle } **0. 系统架构与设计原则**

    ---

    目标边界、四条原则、技术栈总览与"明确不做"的设计。

    [:octicons-arrow-right-24: 阅读](chapter0-architecture.md)

-   :material-file-document-multiple-outline:{ .lg .middle } **1. 资料审计与证据单元**

    ---

    7 份资料的性质、定位字段设计、解析选型、父块悬空事故的修复与邻接回填。

    [:octicons-arrow-right-24: 阅读](chapter1-corpus.md)

-   :material-image-multiple-outline:{ .lg .middle } **2. 多模态证据构建**

    ---

    图资产、视觉路线被迫替换、观察污染事故、图谱条件、BarRef、图文链接。

    [:octicons-arrow-right-24: 阅读](chapter2-multimodal.md)

-   :material-database-outline:{ .lg .middle } **3. 索引构建**

    ---

    BGE-M3 单模型理由、领域分词与词边界事故、嵌入式 Qdrant、重索引策略。

    [:octicons-arrow-right-24: 阅读](chapter3-index.md)

-   :material-magnify:{ .lg .middle } **4. 检索优化**

    ---

    混合召回、RRF、重排、多样性、邻接、toc、图谱、BarRef、失败降级。

    [:octicons-arrow-right-24: 阅读](chapter4-retrieval.md)

-   :material-compass-outline:{ .lg .middle } **5. 查询构建与改写**

    ---

    结构化计划、Step-Back / Multi-query / HyDE、双语对齐、多轮改写、分析 vs 举例分流。

    [:octicons-arrow-right-24: 阅读](chapter5-query.md)

-   :material-text-box-check-outline:{ .lg .middle } **6. 证据核验与生成**

    ---

    三种生成模式、有界补检索、引用绑定、上下文分层、分析优先与图例策略。

    [:octicons-arrow-right-24: 阅读](chapter6-generation.md)

-   :material-brain:{ .lg .middle } **7. SOP 记忆与自动优化**

    ---

    运行轨迹、生命周期、git 版本管理、评价→蒸馏→发布闭环、哪些操作真的生效。

    [:octicons-arrow-right-24: 阅读](chapter7-memory.md)

-   :material-server-outline:{ .lg .middle } **8. 服务与工程化**

    ---

    12 个 API 路由、多轮对话、MCP 适配器、存储、截点遮罩、性能与部署。

    [:octicons-arrow-right-24: 阅读](chapter8-service.md)

-   :material-chart-candlestick:{ .lg .middle } **结构化读图**

    ---

    截图 → 类型化字段 → 读图子查询 → 三层答案；读图不是证据。

    [:octicons-arrow-right-24: 阅读](chapter13-chart.md)

-   :material-compare-horizontal:{ .lg .middle } **All-in-RAG 全链路对照**

    ---

    教程每个环节介绍了什么方法、本项目怎么实现、哪些是没想到、
    哪些是权衡后替代，以及仍未闭环的欠账。

    [:octicons-arrow-right-24: 阅读](chapter12-allinrag-comparison.md)

</div>

## 验证与结果

<div class="grid cards" markdown>

-   :material-chart-box-outline:{ .lg .middle } **评测：把"看起来能用"变成数字**

    ---

    3 个冻结数据集、4 个 harness、宽度消融、答案双臂、盲评，以及机制题 ≈0 的诚实结果。

    [:octicons-arrow-right-24: 阅读](chapter14-evaluation.md)

-   :material-check-decagram-outline:{ .lg .middle } **已完成与待补充**

    ---

    逐项列出已验证的实现与按优先级排列的待补事项。

    [:octicons-arrow-right-24: 查看](chapter9-status.md)

-   :material-play-circle-outline:{ .lg .middle } **运行示例**

    ---

    从环境到全量构建，再到问答、图例、K 线指代与截点上传的真实命令与输出。

    [:octicons-arrow-right-24: 查看](chapter10-examples.md)

-   :material-robot-happy-outline:{ .lg .middle } **AI 评价**

    ---

    一份诚实的自评：优点、短板、10 维度评分与最值得做的三件事。

    [:octicons-arrow-right-24: 查看](chapter11-ai-review.md)

</div>

## 代码地图

| 想看什么 | 从哪读 | 入口 |
|---|---|---|
| 证据与出处契约 | `paknow/evidence/contracts.py` | 第 0、1 章 |
| 摄入流水线 | `paknow/ingest/` | `scripts/ingest_all.py` |
| 检索缝 | `paknow/retrieval/pipeline.py` | 第 4 章 |
| 图谱扩展 | `paknow/graph/expansion.py` | 第 4、12 章 |
| 读图 | `paknow/chart.py` | 第 13 章 |
| 工作流状态机 | `paknow/workflows/graph.py` | 第 5、6 章 |
| SOP 记忆 | `paknow/memory/` | 第 7 章 |
| API 与查看器 | `apps/api/main.py`、`apps/viewer/` | 第 8 章 |
| 评测 | `scripts/eval_*.py`、`scripts/quality_gate.py`、`evals/gold/` | 第 14 章 |
| MCP 适配器 | `integrations/paknow-mcp/` | 第 8 章 |

## 快速开始

```bash
# 1) 依赖（Windows 上 torch/torchvision 必须走 CUDA 索引）
uv venv .venv
uv pip install --python .venv --index-url https://download.pytorch.org/whl/cu128 "torch==2.9.1" "torchvision==0.24.1"
uv pip install --python .venv -e ".[dev]"

# 2) 配置：填 DEEPSEEK_API_KEY；本地模式保持两个 URL 行注释（不要写成空值）
#    PAKNOW_DB_URL → 默认 SQLite（data/paknow.db）
#    QDRANT_URL    → 默认嵌入式 Qdrant（data/objects/qdrant）
cp .env.example .env

# 3) 摄入 + 索引 + 图谱
.venv/Scripts/python.exe scripts/ingest_all.py
.venv/Scripts/python.exe scripts/index_visual.py
.venv/Scripts/python.exe scripts/build_taxonomy.py --apply

# 4) 启动服务（8000 归 API；文档站用 8080）
.venv/Scripts/python.exe -m uvicorn apps.api.main:app --port 8000
```

!!! note "文档与代码的关系"
    本文档是**实施后的技术说明**，以实际代码、数据库计数与运行结果为准：
    记录实际发生了什么、为什么这么做、哪些坑踩过并修好了。设计过程中的讨论稿不随仓库发布。
    所有规模数字来自本地库快照（2026-10-25，父块回填后），重算方法见 [评测](chapter14-evaluation.md) §8。
