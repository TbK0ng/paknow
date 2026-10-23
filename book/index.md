<div class="paknow-hero">
  <h1>PAknow</h1>
  <p class="paknow-sub">
    面向本地价格行为资料的多模态 RAG：<strong>可追溯出处</strong>的知识问答、
    带条件的主张图谱、派生视觉索引，以及<strong>可验证、可版本化、可回滚</strong>的程序性记忆（SOP）。
  </p>
  <div class="paknow-badges">
    <span class="paknow-badge">91,235 证据单元</span>
    <span class="paknow-badge">927 图资产</span>
    <span class="paknow-badge">2,694 概念 · 1,888 主张</span>
    <span class="paknow-badge">674 K 线指代</span>
    <span class="paknow-badge">SOP 生命周期 + git</span>
  </div>
  <div class="paknow-cta">
    <a class="paknow-btn" href="chapter0-architecture/">开始阅读</a>
    <a class="paknow-btn-ghost" href="chapter10-examples/">运行示例</a>
    <a class="paknow-btn-ghost" href="https://github.com/TbK0ng/paknow">GitHub 仓库</a>
  </div>
</div>

<div class="paknow-features">
  <div class="paknow-feature">
    <div class="paknow-feature-icon">🔗</div>
    <h3>可追溯</h3>
    <p>每个 <code>[n]</code> 绑定证据编号，最终落到「书 → 章 → 图 → 页」。</p>
  </div>
  <div class="paknow-feature">
    <div class="paknow-feature-icon">🧪</div>
    <h3>可验证</h3>
    <p>图谱主张带条件，视觉观察标记为派生材料，SOP 需要回放证据才能晋升。</p>
  </div>
  <div class="paknow-feature">
    <div class="paknow-feature-icon">🕘</div>
    <h3>可回滚</h3>
    <p>记忆以 YAML + git 管理，每次变更可 diff、可 blame、可回退。</p>
  </div>
</div>

## 设计流程

按 RAG 系统的构建顺序展开。每一章回答：这个环节要解决什么、为什么选这个方案、为什么不选另一个。

<div class="grid cards" markdown>

-   :material-sitemap-outline:{ .lg .middle } **0. 系统架构与设计原则**

    ---

    目标边界、四条原则、技术栈总览与"明确不做"的设计。

    [:octicons-arrow-right-24: 阅读](chapter0-architecture.md)

-   :material-file-document-multiple-outline:{ .lg .middle } **1. 资料审计与证据单元**

    ---

    7 份资料的性质、定位字段设计、解析选型、父块悬空事故与邻接回填。

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

    单次结构化计划、Step-Back / Multi-query / HyDE、双语对齐、分析 vs 举例分流。

    [:octicons-arrow-right-24: 阅读](chapter5-query.md)

-   :material-text-box-check-outline:{ .lg .middle } **6. 证据核验与生成**

    ---

    两种生成模式、有界补检索、引用绑定、上下文分层、分析优先与图例策略。

    [:octicons-arrow-right-24: 阅读](chapter6-generation.md)

-   :material-brain:{ .lg .middle } **7. SOP 记忆与自动优化**

    ---

    运行轨迹、生命周期、git 版本管理、评价→蒸馏→发布闭环、生效方式与局限。

    [:octicons-arrow-right-24: 阅读](chapter7-memory.md)

-   :material-server-outline:{ .lg .middle } **8. 服务与工程化**

    ---

    API、存储、截点遮罩、git 实践、性能实测、部署与文档站。

    [:octicons-arrow-right-24: 阅读](chapter8-service.md)

-   :material-compare-horizontal:{ .lg .middle } **All-in-RAG 全链路对照**

    ---

    教程每个环节介绍了什么方法、本项目怎么实现、哪些是没想到、
    哪些是权衡后替代，以及仍未闭环的欠账。

    [:octicons-arrow-right-24: 阅读](chapter12-allinrag-comparison.md)

</div>

## 状态与验证

<div class="grid cards" markdown>

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

## 快速开始

```bash
# 1) 依赖（Windows 上 torch/torchvision 必须走 CUDA 索引）
uv venv .venv
uv pip install --python .venv --index-url https://download.pytorch.org/whl/cu128 "torch==2.9.1" "torchvision==0.24.1"
uv pip install --python .venv -e ".[dev]"

# 2) 配置（复制 .env.example 并填入 DEEPSEEK_API_KEY）
cp .env.example .env

# 3) 摄入 + 索引
.venv/Scripts/python.exe scripts/ingest_all.py
.venv/Scripts/python.exe scripts/index_visual.py

# 4) 启动服务
.venv/Scripts/python.exe -m uvicorn apps.api.main:app --port 8000
```

!!! note "文档与代码的关系"
    本文档是**实施后的技术说明**，以实际代码、数据库计数与运行结果为准：
    记录实际发生了什么、为什么这么做、哪些坑踩过并修好了。设计过程中的讨论稿不随仓库发布。
