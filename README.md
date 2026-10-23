# PAknow

多模态检索、证据图谱与可验证 SOP 记忆系统。对本地 PDF/EPUB 价格行为资料做
**可追溯出处**的知识问答、上传图表分析，并保留程序性记忆（SOP）的生命周期。

> 技术文档站：按 RAG 设计流程讲清每个环节的技术选型与实际结果（`book/`，MkDocs）。
> 回测/行情数据与人工金标准评测按实施决策暂缓。

## 能力

- **知识问答**：中英术语双语检索（BGE-M3 dense+sparse 混合 + RRF 融合），
  答案逐项绑定证据编号，程序渲染为可点击出处（`《书名》→ 章节 → 图号 → 页`）。
- **上传图表分析**：`deepseek-flash`（vision）先记录可见事实再检索条件/反例。
- **证据图谱**：概念/带条件主张/边（`SUPPORTED_BY`、`REQUIRES_CONTEXT` 等），
  有界 BFS 检索。
- **SOP 记忆**：`observed → candidate → replay_validated → shadow → active →
  superseded/retired` 生命周期，离线对照回放后才可晋升。
- **来源定位**：PDF 保留物理页 / 章内印刷页 / 章节 / 图号分别存储；EPUB 保留
  spine 阅读顺序 + 锚点 + 图注邻接。

## 架构

```
本地 PDF/EPUB → 解析(EPUB 原生 / PDF PyMuPDF) → 证据单元+图资产(内容寻址)
   → 索引(BGE-M3 dense+sparse / 目录 / Qwen 视觉观察)  → 多路召回+RRF融合
   → 证据充分性检查(有界补检索) → deepseek-flash 生成(逐项引用) → RunTrace
```

关键组件：

| 层 | 模块 |
|---|---|
| 契约 | `paknow/evidence|graph|memory/contracts.py`（Pydantic） |
| 持久化 | `paknow/db/models.py`（SQLAlchemy，默认 SQLite） |
| 摄入 | `paknow/ingest/{manifest,epub,pdf,chunk,ocr,persist,pipeline}.py` |
| 检索 | `paknow/retrieval/{embedding,tokenize,qdrant_store,index,search,visual}.py` |
| 图谱 | `paknow/graph/{extract,retrieve}.py` |
| 工作流 | `paknow/workflows/{router,evidence,generate,graph}.py`（LangGraph） |
| 记忆 | `paknow/memory/{trace,sop}.py` |
| 服务 | `apps/api/main.py`（FastAPI）+ `apps/viewer/index.html` |

## RAG 全链路对照：All-in-RAG 的方法清单与本项目取舍

> 对照 [Datawhale All-in-RAG](https://datawhalechina.github.io/all-in-rag/) 的完整教程链路。
> 逐环节回答三个问题：**教程介绍了哪些方法、PAknow 实际怎么实现、当前选择是“没想到”
> 还是“权衡后替代”，为什么**。
>
> 方法级审计（含代码位置、数据库计数、未接线清单）见
> [`book/chapter12-allinrag-comparison.md`](book/chapter12-allinrag-comparison.md)。

### 结论先行

教程给的是“通用文本 RAG 的标准件”；PAknow 在**证据定位、多模态派生、条件化图谱、
SOP 记忆**上做了加法定制，在**评估、索引调优、上下文压缩、元数据过滤、Agentic
工具生态**上做了减法或欠账。加法多数是语料逼出来的；减法多数有环境或成本理由，
但**评估、metadata 过滤、压缩**是真实缺口，不能包装成设计。

| 环节 | All-in-RAG 的代表方法 | PAknow 的实现 | 判定 |
|---|---|---|---|
| 架构范式 | Naive / Advanced / Modular、RAG vs 微调、框架 vs 裸写 | LangGraph 固定状态机 + 自定义证据契约 | **采用 Modular**；不做自主多工具 Agent |
| 数据加载 | 9 种 loader、Unstructured 元素、Docling/MinerU/OCR | 原生 EPUB + PyMuPDF；块/bbox/图注/物理页/印刷页 | **采用且更强**；表格未抽取，Docling/MinerU 未实测 |
| 分块 | 固定/递归/语义/结构/句子窗口/父子块 | 单元优先，超长切 600–1200 字符；邻接 81,809 条 | **等价采用结构分块+邻接**；父子块实测失败后替代 |
| 嵌入 | MTEB 选型、dense/sparse、CLIP/Visualized-BGE/ColPali | BGE-M3 dense+sparse + 领域分词；Qwen2.5-VL 观察 | **主链路采用**；ColPali 尝试失败后转观察式，无 image-to-image |
| 向量库/索引 | FAISS/Chroma/Milvus/Qdrant、FLAT/IVF/HNSW/DiskANN | Qdrant 嵌入式；命名向量 + RRF | **采用 Qdrant**；HNSW/量化未调优 |
| 检索 | 混合、RRF/加权、重排、压缩、校正、上下文扩展 | RRF + cross-encoder + 多样性 + 邻接 + 有界补检索 | **主链路采用**；压缩未做，metadata 过滤未接线 |
| 查询构建 | 多查询、Step-Back、HyDE、LLM/向量路由、Text2SQL/Cypher | 单次 QueryPlan + 消歧 + SOP 补充查询 | **改写采用**；单次调用替代多步 Agent；Text2SQL/Cypher 不适用 |
| 生成 | OutputParser/Pydantic、Function Calling、GBNF、引用 | `chat_json` + Pydantic + `[n]` 后端绑定 | **结构化与引用更强**；Function Calling/流式未做 |
| 评估 | RAG Triad、Precision/Recall/MRR/MAP、Ragas、Phoenix | 程序化 trace 信号 + SOP 闭环 | **最大缺口**：没有质量评测，SOP `active` 关闭 |
| 图谱 | KG 构建、Neo4j/Cypher、GraphRAG/LightRAG/FRAG/GraphIRAG | 条件化概念/主张/边 + 有界 BFS + 生命周期环 | **局部图检索采用**；无社区摘要/全局 GraphRAG |
| Agentic | Plan→Retrieve→Critique→Refine、工具生态、多 Agent | 受控核验-补检索，最多 3 轮 | **受控采用**；无自主工具选择/多 Agent |
| 记忆/工程 | 教程仅 trace；PAknow 额外做 SOP | SOP 生命周期 + git + FastAPI + 查看器 | **超出教程**；UserFact/ImportJob 未接线，无流式/缓存 |

### 未闭环 / 未接线清单

- **评测**：没有冻结的金标准、RAG Triad、Recall@k、Ragas/Phoenix、B0–B7 消融；
  这是 SOP 无法晋升 `active` 的直接原因。
- **元数据过滤**：`QueryPlan.metadata` 已生成，但检索层没有消费。
- **空路由**：`visual_page` / `numeric` 出现在 QueryPlan 里，主链路没有实现。
- **视觉集合**：`page_visual` 集合 0 点；当前只有“文本描述找图”，没有图像相似检索。
- **上下文压缩**：没有 LLMChainExtractor/Filter；靠 14 条证据截断 + 4 条邻接控制窗口。
- **索引调优**：没有 HNSW 参数、量化、Recall@k/延迟基准。
- **工具调用**：没有 Function Calling；当前能力没有需要模型执行的外部工具。
- **两张表未接线**：`user_facts`、`import_jobs` 有 schema，主链路未使用。
- **体验**：无流式、无查询缓存、无鉴权/多租户。

### 为什么这些减法成立

| 约束 | 对设计的影响 |
|---|---|
| 语料是 7 本图文混排、条件密集的交易书 | 证据单元必须带页/图/bbox/质量；通用 chunk 技巧收益低 |
| 离线、无 Docker、16GB 显存、Blackwell | Qdrant 嵌入式、PyMuPDF、Qwen2.5-VL；Milvus/ColPali 不可行 |
| 每个结论要能回到原文 | 引用不交给模型生成；`[n]` 由后端映射到 `evidence_id` |
| 派生材料不能冒充原文 | 视觉观察/图谱主张/SOP 都不可直接引用 |
| 记忆必须可验证、可回滚 | SOP 用 YAML + git；自动闭环最多到 `shadow` |
| 延迟/成本可控 | 路由/核验/生成各一次云端调用；不做多步 Agent |

### 最值得补的三件事

1. **评测闭环**：金标准 + RAG Triad/Ragas + Recall@k + 回归门槛。先证明重排/图谱/toc/
   邻接各自有没有增量，再谈 SOP 晋升。
2. **把计划字段接线**：`metadata` 过滤、`visual_page`/`numeric` 路由，要么实现，
   要么从 QueryPlan 删除，避免“字段存在但主链路不消费”。
3. **压缩与索引基准**：上下文压缩 + HNSW/量化/延迟-召回曲线。当前 8 万点已接近
   嵌入式 Qdrant 的舒适区，需要用数据决定是否切 server。

## 技术文档站

仓库内含一套按 RAG 设计流程组织的技术文档（`book/`，MkDocs Material），
讲清楚每个环节的技术选型、为什么选/不选、已完成与待补充、运行示例与 AI 评价：

```bash
.venv/Scripts/python.exe -m mkdocs serve      # 本地预览 http://127.0.0.1:8000
.venv/Scripts/python.exe -m mkdocs build      # 构建到 site/
.venv/Scripts/python.exe -m mkdocs gh-deploy  # 发布到 GitHub Pages
```

## 快速开始

```bash
# 1. 依赖（Windows 上 torch/torchvision 必须用 CUDA index，PyPI 默认是 CPU 版）
uv venv .venv
uv pip install --python .venv --index-url https://download.pytorch.org/whl/cu128 "torch==2.9.1" "torchvision==0.24.1"
uv pip install --python .venv -e ".[dev]"
.venv/Scripts/pre-commit.exe install          # 启用 git hooks（提交前跑 pytest + scratch 校验）

# 2. 配置 .env（复制 .env.example，填入 DEEPSEEK_API_KEY）
cp .env.example .env   # HF_HOME 默认 E:\use_model（模型缓存迁出 C 盘）

# 3. 全量摄入 + 索引（首次会下载 BGE-M3 ~2.2GB）
.venv/Scripts/python.exe scripts/ingest_all.py

# 4. 视觉观察索引（Qwen2.5-VL，首次下载 ~8GB）
.venv/Scripts/python.exe scripts/index_visual.py

# 5. 启动服务
.venv/Scripts/python.exe -m uvicorn apps.api.main:app --port 8000
# 打开 http://localhost:8000 提问
```

## 默认存储（离线零依赖）

- 权威元数据：SQLite（`data/paknow.db`），生产切 Postgres 见 `.env` 注释。
- 向量：Qdrant **嵌入式本地模式**（`data/objects/qdrant`），生产切 server。
- 派生对象（图片/中间件）：`data/objects/` 内容寻址。
- 云端：`deepseek-flash`（DeepSeek-V4.1-Flash，text+image），`ModelGateway` 可换。

## 接口（`apps/api/main.py`）

- `POST /query` — 问答（路由→检索→核验→生成→记录）
- `POST /charts` — 上传截图分析（vision）
- `GET /evidence/{id}` / `GET /sources/{doc_id}` — 证据/来源定位
- `GET /sources/{doc_id}/page/{page}/image` — PDF 页渲染（查看器用）
- `POST /search` — 只检索不生成（供 Agent 补证/核对）
- `GET /documents` — 资料库文档目录
- `GET /figures/{figure_id}/image` — 图资产原图
- `GET /runs/{run_id}` — 审计 trace；`GET /sops` — SOP 版本

## Pi / MCP 集成

仓库内提供独立的 `integrations/paknow-mcp/`：一个不导入 PAknow 内部模块的
MCP 适配器，通过 HTTP 调用上述接口。它暴露 `paknow_ask`、`paknow_chart`、
`paknow_search`、`paknow_evidence`、`paknow_documents`、`paknow_source`、
`paknow_page_image`、`paknow_figure_image`、`paknow_run`、`paknow_sops`、
`paknow_health`，并附带通用的 `skills/paknow/SKILL.md`（意图路由、引用保真、
能力边界、失败处理）。

```bash
cd integrations/paknow-mcp
uv pip install -e .
paknow-mcp                       # stdio
# 或 streamable HTTP
paknow-mcp --transport streamable-http --port 8765
```

发布包包含 wheel、sdist、skill、`MANIFEST.json` 与 `SHA256SUMS`；构建与
解包验证脚本见 `integrations/paknow-mcp/scripts/`。

## 验证

```bash
.venv/Scripts/python.exe scripts/smoke_full.py   # 摄入+索引+检索
.venv/Scripts/python.exe scripts/smoke_query.py  # 端到端问答(含引用)
.venv/Scripts/python.exe scripts/smoke_ingest.py # EPUB 解析
.venv/Scripts/python.exe scripts/smoke_pdf.py    # PDF 第93页/图4.5/印刷页7
```

## 已核验的端到端结果（示例）

问题「双底多头旗形的两个低点必须一样吗？」→ 答案引用原文
"about the same / around the same price / bottoms do not have to be exactly
at the same price"，并区分 micro double bottom bear flag 反例，`uncertainty`
明确价差阈值未说明。

## 未完成 / 待补

- 真实行情数据 + 回测（暂缓）
- 人工金标准评测 + 消融（暂缓；它决定 SOP 能否晋升 `active`）
- 视觉观察覆盖 927 张图资产（观察式索引，非图像相似检索）
- 证据图谱：2,694 概念 / 1,888 主张 / 3,425 边

## 视觉方案说明（Blackwell 适配）

ColPali（PaliGemma）在本机 **RTX 5060 Ti（Blackwell sm_120）+ torch 2.9.1** 上触发
CUDA 内核越界崩溃；ColQwen2 checkpoint 又依赖 colpali_engine（要求 transformers 4.x）。
故视觉路由改用 **Qwen2.5-VL-3B**（Blackwell 兼容、transformers 5.x 原生）：对图资产生成
结构化观察，观察文本作为**派生索引材料**（非权威来源）进入同一 dense+sparse 索引。
上传图表分析走 cloud vision（deepseek-flash）。
