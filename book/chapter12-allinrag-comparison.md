---
description: "对照 All-in-RAG 教程逐环节复盘：哪些方法已采用、哪些权衡后替代、哪些想到但没接线。"
---

# 全链路对照：All-in-RAG 的方法清单与本项目取舍

> 对照对象：Datawhale [All-in-RAG](https://datawhalechina.github.io/all-in-rag/)（教程站与
> `datawhalechina/all-in-rag` 仓库，2026-10-19 快照）。
>
> 本项目：PAknow @ `5886d95`。所有“当前实现”都能在仓库代码中找到对应；结构数字来自本地
> `data/paknow.db` 快照（2026-10-24，`evidence_text` 81,809 点；`page_visual` 0 点），
> 评测数字来自 `.scratch/` 下的运行产物（随仓库提交，含逐题记录），口径见 [评测](chapter14-evaluation.md)。
>
> 这一章不是“功能对齐清单”，而是一次逐环节复盘：教程介绍了哪些方法、实际怎么实现、
> 当前选择是**没想到**、**权衡后替代**，还是**想到了但没接线**。

## 0. 判定口径

| 判定 | 含义 |
|---|---|
| **已采用** | 功能等价或更强，主链路实际运行 |
| **权衡替代** | 看过/考虑过，因语料、硬件、成本、可验证性选择另一条路；理由可复述 |
| **接线缺口** | 字段、表、路由或脚本已经存在，但主链路没有消费；属于“想到了没落地” |
| **未实现 / 暂缓** | 明确的能力缺口；不把“没做”说成“不需要” |
| **不适用** | 与当前任务边界无关（如无结构化行情库时的 Text2SQL） |

**一句话结论**：All-in-RAG 给的是“通用文本 RAG 的标准件”；PAknow 在**证据定位、
多模态派生、条件化图谱、SOP 记忆**上做了加法定制，在**评估、索引调优、上下文压缩、
元数据过滤、Agentic 工具生态**上做了减法或欠账。加法多数是语料逼出来的；减法多数
有环境或成本理由，但其中三项是真实缺口，不能包装成设计。

## 1. 结论矩阵

| 环节 | 教程方法密度 | PAknow 状态 | 一句话 |
|---|---|---|---|
| 架构范式 | Naive / Advanced / Modular、RAG vs 微调、框架 vs 裸写 | 已采用 Modular；受控 Agentic | LangGraph 固定状态机，不做自主多工具 Agent |
| 数据加载 | 9 种 loader、Unstructured 元素体系、OCR/版面解析 | 已采用且更强；部分未覆盖 | 原生 EPUB + PyMuPDF 保留结构/坐标；表格未抽取 |
| 分块 | 固定/递归/语义/结构/句子窗口/父子块 | 已采用等价实现；父子块失败后替代 | 单元优先 + 阅读顺序邻接；未做语义分块 A/B |
| 嵌入 | MTEB 选型、dense/sparse、CLIP/Visualized-BGE/ColPali | 已采用 dense+sparse；视觉路线替代 | BGE-M3 + 自定义稀疏分词；无 image-to-image |
| 向量库/索引 | FAISS/Chroma/Milvus/Qdrant、FLAT/IVF/HNSW/DiskANN | 已采用 Qdrant；调优欠账 | 命名向量 + RRF；HNSW/量化未基准 |
| 检索 | 混合、RRF/加权、重排、压缩、校正、上下文扩展 | 主链路已采用；压缩/元数据过滤缺口 | RRF + cross-encoder + 邻接 + 有界补检索 |
| 查询构建 | 多查询、Step-Back、HyDE、LLM/向量路由、Text2SQL/Cypher | 改写已采用；结构化查询未用 | 单次 QueryPlan 替代多步 Agent；metadata 未接线 |
| 生成 | OutputParser/Pydantic、Function Calling、GRNF、引用 | 结构化输出与引用更强；工具调用缺口 | `chat_json` + Pydantic + `[n]` 后端绑定 |
| 评估 | RAG Triad、Precision/Recall/MRR/MAP、Ragas、Phoenix | 已起步，仍未形成门槛 | 自建三数据集 + 确定性硬约束 + 盲评；不接公开榜单 |
| 图谱 | KG 构建、Neo4j/Cypher、GraphRAG/LightRAG/FRAG/GraphIRAG | 局部图检索已采用 | 条件化主张 + 有界沿边扩展；无社区摘要/全局 GraphRAG |
| Agentic | Plan→Retrieve→Critique→Refine、工具生态、多 Agent | 受控采用 | 有核验-补检索；无自主工具选择/多 Agent |
| 记忆/工程 | 教程仅 trace；PAknow 额外做 SOP | 超出教程 | SOP 生命周期 + git；active 被评测缺口卡住 |

## 2. 架构与范式

| All-in-RAG 方法 | PAknow 实现 | 判定 | 理由 |
|---|---|---|---|
| Naive RAG：索引 → 检索 → 生成 | 有完整链路，但多了证据核验/补检索/引用绑定 | 已采用并超出 | 交易资料需要可追溯出处，线性链路不够 |
| Advanced RAG：查询改写 + 重排 | QueryPlan + RRF + cross-encoder rerank | 已采用 | 与教程一致 |
| Modular RAG：动态路由/查询转换/多路融合 | LangGraph 固定状态机 + `routes` / `evidence_needs` | 已采用（受控） | 固定流程的可控性优先于自由编排 |
| RAG vs 微调：先提示工程，再 RAG，最后微调 | 只用 RAG + 云端生成；不微调 | 已采用原则 | 7 本书、无标注对、版权与算力都不支持微调 |
| LangChain / LlamaIndex 快速集成 | LangGraph 编排 + 自写契约/检索/生成；不是 LangChain 全家桶 | 权衡替代 | 需要 bbox/图号/页号/质量标记等自定义契约 |
| 不依赖框架的原生开发 | 大部分模块原生实现 | 已采用 | 便于定位“证据”语义 |
| FastGPT / Dify 低代码 | 未使用 | 不适用 | 无法满足逐项引用与本地证据定位 |
| 多步 Agent / Agentic RAG | 单次 QueryPlan + 核验器 + 最多 3 轮补检索 | 权衡替代 | 每多一步多一次延迟与失败点；固定流程足够 |
| 四步 MVP（数据/索引/检索/生成） | 四步都有，另加证据层、图谱、SOP | 已采用并扩展 | 教程是起点，不是上限 |

**复盘**：这里不是“没想到 Agentic”。教程第 7 章明确给了 Plan→Retrieve→Critique→Refine；
PAknow 保留了 Critique/Refine，只去掉了自主工具选择。原因是当前任务只有“文本/图谱/
目录/图注”四类固定证据源，让模型自由选工具不会增加信息，只会增加不确定性。

## 3. 数据加载与解析

| All-in-RAG 方法 | PAknow 实现 | 判定 | 理由 |
|---|---|---|---|
| TextLoader / DirectoryLoader | `manifest.register_document` + `ingest_all.py` 批量摄入 | 已采用等价 | 输入是 PDF/EPUB，不是纯文本 |
| Unstructured `partition` 及元素体系 | 自定义 `paragraph/heading/caption/footer`；保留 bbox、页、章节、图号 | 权衡替代 | 需要元素级坐标与图注归属；通用元素分类不够 |
| `hi_res` / `ocr_only` / PaddleOCR | `ingest/ocr.py` 保留候选检测，主链路未接线 | 权衡替代 | 当前 7 份资料 0 页需要 OCR（均有原生文本层） |
| PyMuPDF4LLM / Marker / MinerU / Docling / LlamaParse | PDF 用 PyMuPDF（fitz）原生解析 | 权衡替代 / 部分未对照 | PyMuPDF 同时给文本块 bbox、图片 bbox、页渲染；Docling/MinerU 只在计划中列为对照，未实际跑同页评测 |
| 表格 / 公式 / 代码元素 | 未专门抽取；表格文字可能作为普通块进入 | **未实现** | 当前语料以正文+图表为主；扩语料前是真实缺口 |
| Title / NarrativeText / ListItem | 标题按字号检测；正文按块；列表未单独建模 | 部分采用 | 价格行为书以段落为主，列表语义价值低 |
| FigureCaption / Image | `FigureAsset` + `caption_ids/explanation_ids` + 几何/标签回填 | 已采用且更强 | 教程只到“有图元素”，PAknow 做到图-注-解释双向链接 |
| Header / Footer / PageNumber | 页脚识别后不索引、不删；物理页/印刷页分开 | 已采用且更强 | 页脚 9,426 条，索引会污染 top-k |
| PageBreak / 阅读顺序 | EPUB spine + PDF 块顺序 + 邻接回填 | 已采用 | 结构是引用定位的前提 |
| 元数据（来源/页码/作者） | `DocumentRow` + `SourceLocator` + `chapter_path` | 已采用且更强 | 引用要落到“书→章→图→页” |
| 页级作业清单/重试 | `ImportJobRow` 契约存在，主链路未使用 | 接线缺口 | 大 PDF 的失败恢复还没落地 |

**复盘**：解析路线不是“随便选 PyMuPDF”。EPUB 原生解析是为了保留 `<a href="#fig4.5">`
这类作者显式图引用；PDF 用 PyMuPDF 是为了坐标。两者都不是通用 loader 能直接替代的。
真正欠账是**表格**与**Docling/MinerU 同页对照**：计划里写了，实施时没跑。

## 4. 分块与证据单元

| All-in-RAG 方法 | PAknow 实现 | 判定 | 理由 |
|---|---|---|---|
| 固定大小 / `CharacterTextSplitter` | 未使用 | 权衡替代 | 会切断条件句、图号指代和图文绑定 |
| 递归字符 / `RecursiveCharacterTextSplitter` | 未使用；超长单元按句末切成 ≤1200 字符 | 权衡替代 | 解析器已给出段落/标题/图注边界，递归切分是二次破坏 |
| 语义分块 / `SemanticChunker` | 未使用 | 未实现（未做 A/B） | 嵌入成本 + 可能把“条件+结论”切散；结构边界优先，但没有实验证明更好 |
| Markdown 标题分块 | EPUB 原生标题树；PDF 按字号/目录推断 | 已采用等价 | 不依赖 Markdown 中间格式 |
| Unstructured `basic` / `by_title` | 自定义块分类 + 章节路径 | 权衡替代 | 需要图注/页脚/bbox 的细粒度控制 |
| SentenceWindowNodeParser | 检索后展开相邻单元（最多 4 条） | 已采用等价 | 教程用 metadata 窗口；PAknow 用阅读顺序邻接，避免索引膨胀 |
| 父子块：小块检索、大块生成 | 子块 `parent_id` 指向原单元；但原单元未持久化，1,670/1,670 悬空 | **权衡替代 + 事故** | 实测父块不可解析，改为阅读顺序邻接（91,235 条全覆盖） |
| 元数据注入（Header 1/2/3） | `chapter_path`、`figure_label`、`pdf_page`、`bbox`、`provenance` | 已采用且更强 | 引用与过滤都依赖这些字段 |
| 智能去重 / 合并父文档 | 80 字归一化哈希 + 每书最多 5 条 | 已采用等价 | 不做 embedding 聚类；当前重复主要是同段/多版本 |
| 多表递归检索 / PandasQueryEngine | 未使用 | 不适用 | 没有多工作表结构化数据；也有代码执行安全风险 |
| 表格感知分块 | 未实现 | **未实现** | 当前语料表格少；扩语料要补 |
| 重叠（overlap） | 不设重叠；用邻接扩展补上下文 | 权衡替代 | 重叠会重复计分；邻接在检索后按需展开，成本更低 |

**复盘**：分块是 PAknow 与教程差异最大的地方之一。教程从“纯文本”出发，所以固定/递归/
语义/结构分块是主轴；PAknow 从“已经结构化的证据单元”出发，分块问题变成“如何切超长
段落 + 如何补上下文”。父子块是一次真实的失败：不是没想到，而是做了、测了、发现
`parent_id` 全部悬空，才换成邻接。这个事故比任何分块技巧都更能说明“数据层验证”的必要性。

## 5. 嵌入与多模态

| All-in-RAG 方法 | PAknow 实现 | 判定 | 理由 |
|---|---|---|---|
| MTEB 榜单 + 私有评测集选型 | 未跑 MTEB/私有集；按多语言 + dense/sparse + 本地 GPU 选 BGE-M3 | 权衡替代（缺证据） | 需求先于榜单；但没有做同语料模型对比，是方法欠账 |
| BGE-small-zh / bge-base-zh | BGE-M3（1024 维 dense）+ 自研 TF 稀疏 | 已采用且更强 | 单模型多语；稀疏侧没有用模型输出，见第 3 章 |
| Dense + sparse 双向量 | Qdrant 同点命名向量 `dense`/`sparse` | 已采用 | 与教程第 4 章一致 |
| BM25 | 未用 BM25；用自研 TF 稀疏向量（BGE-M3 只出 dense）+ 领域分词 | 权衡替代（非等价） | 领域缩写/图号保护更关键；但没有 IDF/长度归一化 |
| 对比学习 / 度量学习 / 领域微调 | 未微调 | 未实现 | 无标注正负对；7 本书不足以安全微调 |
| CLIP / Visualized-BGE / bge-visualized-m3 | 未使用 | 权衡替代 | 需要联合图文向量；当前视觉路线改为 VLM 观察 |
| ColPali / ColQwen2 页面多向量 | 尝试后失败：Blackwell CUDA 崩溃 / `colpali_engine` 依赖 transformers 4.x | 权衡替代 | 硬件/依赖约束，不是“没想到” |
| 图像到图像相似检索 | 未实现；`page_visual` 集合 0 点 | **未实现** | 只能做“文本描述找图”，不能做“以图搜图” |
| VLM 观察式索引 | Qwen2.5-VL-3B 生成 `derived_observation`，进同一 dense+sparse 索引 | 已采用且是替代方案 | Blackwell 兼容；观察文本不是权威原文 |
| 观察文本的引用边界 | `provenance=derived_observation`，生成时移出可引用证据，进“图读辅助”块 | 已采用且更强 | 防止派生材料冒充原文；725 条 prompt 污染已清洗 |
| 上传图表分析 | 云端 vision + `cutoff` 右侧遮罩 + 先观察后检索 | 超出教程 | 教程的多模态是“检索图”，不是“分析用户图” |
| 页面视觉索引 | `COLLECTION_VISUAL` 已建但 0 点；`search_pages` 实际走文本观察 | 接线缺口 | 多向量页面检索没有落地 |

**复盘**：视觉是“尝试后被迫替代”，不是跳过。ColPali 的失败有明确错误形态（CUDA kernel
越界、依赖冲突），Qwen 观察是可运行方案，但能力边界必须写清：**观察式检索不是图像
相似检索**。这也是 README 里专门标注“非图像相似检索”的原因。

## 6. 向量库与索引

| All-in-RAG 方法 | PAknow 实现 | 判定 | 理由 |
|---|---|---|---|
| FAISS 本地索引 | 未使用 | 权衡替代 | 无 payload 过滤、混合检索要自己拼 |
| Chroma / Pinecone / Weaviate | 未使用 | 不适用/未评估 | 需要离线、嵌入式、可切 server；当前无多租户/云服务需求 |
| Milvus Standalone（Docker） | 未使用 | 权衡替代 | 本机无 Docker；Qdrant 嵌入式零启动 |
| Qdrant | Qdrant 嵌入式 + 命名向量 + RRF | 已采用 | dense+sparse 同点、payload 过滤、URL 可切 server |
| Collection / Partition / Alias | Qdrant collection；无 partition/alias 切换 | 权衡替代 | 当前单集合、单版本；重建靠脚本 |
| FLAT / IVF / HNSW / DiskANN | Qdrant 默认 HNSW；未显式调参 | **未调优** | 8 万点规模默认可用；但没有召回/延迟基准 |
| 量化（SQ8/PQ/二进制） | 未使用 | 未实现 | 当前内存足够；没有基准就不假装需要 |
| 过滤检索 | 仅 `caption_search` 使用 `block_type=caption` 过滤 | 部分采用 | 主检索未消费 `QueryPlan.metadata` |
| 范围检索 | 未使用 | 未实现 | 当前没有“相似度阈值区间”类需求 |
| 分组检索（按文档去重） | 80 字哈希 + 每书配额 | 已采用等价 | 在应用层做，避免依赖特定库的 grouping |
| 混合检索 RRFRanker / WeightedRanker | Qdrant `FusionQuery(RRF)` | 已采用 | RRF 对量纲不敏感；加权需归一化/调参 |
| 索引缓存 | Qdrant 持久化；模型 `lru_cache` | 已采用等价 | 没有单独的“启动秒开”索引缓存层 |
| 增量索引 / 重索引 | `index_doc` 增量；`reindex_sparse.py` 只重算 sparse | 已采用且更强 | 分词器变更后必须重索引，这是实际踩过的坑 |
| 多向量页面集合 | 空集合 | 接线缺口 | 见 5 节 |

**复盘**：选 Qdrant 不是“教程没讲 Milvus”。教程第 3 章把 Milvus 讲得很完整，但本机没有
Docker，且 Qdrant 的命名向量刚好满足“同点 dense+sparse + RRF”。真正的欠账是**索引
调优**：没有 HNSW 参数、量化、Recall@k/延迟基准。8 万点规模下这不是瓶颈，但不应把
“默认能用”写成“已经优化”。

## 7. 检索

| All-in-RAG 方法 | PAknow 实现 | 判定 | 理由 |
|---|---|---|---|
| 稀疏 vs 密集、混合检索 | 每路 40 条，Qdrant RRF | 已采用 | 与教程一致 |
| RRF vs 加权线性融合 | RRF | 已采用 | dense 余弦与 sparse 点积量纲不同；RRF 只看排名 |
| 元数据过滤器 / SelfQueryRetriever | `QueryPlan.metadata` 已生成，检索未消费 | **接线缺口** | 想到了但没落地；主链路只有 caption 过滤 |
| 多查询分解（Multi-query） | `sub_queries` 2–4 条 | 已采用 | 覆盖定义/条件/反例/统计角度 |
| Step-Back | `step_back` | 已采用 | 解决术语抽象层级不对齐 |
| HyDE | `hyde`，放在查询集最后 | 已采用 | 只做召回探针，不做最终答案 |
| LLM 意图路由 | 单次 QueryPlan | 已采用 | 一次调用产出意图/路由/证据需求 |
| 嵌入相似性路由 | 未使用 | 权衡替代 | 单次 LLM 调用已覆盖；向量路由省延迟但灵活性低 |
| Text2SQL | 未实现 | 不适用 | 没有行情/结构化库；回测边界外 |
| Text2Cypher | 未实现 | 不适用/权衡替代 | 没有 Neo4j；图检索用 SQLAlchemy 有界 BFS |
| RRF / RankLLM / Cross-Encoder / ColBERT 重排 | `bge-reranker-v2-m3` cross-encoder，top20 | 已采用一种 | 本地 GPU、延迟可控 |
| LLM 重排 | 未使用 | 权衡替代 | 每问多一次云端调用；成本/延迟不划算 |
| ColBERT 重排 | 未使用 | 权衡替代 | late-interaction 索引/存储与嵌入式 Qdrant 冲突 |
| 上下文压缩（LLMChainExtractor/Filter/EmbeddingsFilter） | 未实现 | **未想到/未实现** | 当前用候选截断（14 条）+ 邻接（4 条）控制窗口，但没有做内容压缩 |
| 校正（Correcting） | `check_sufficiency` + `next_query` + 最多 3 轮 | 已采用 | 只补明确缺口；核验器失败 fail-open |
| 句子窗口 / 父块上下文 | 邻接扩展（最多 4 条）；父块代码保留但数据无效 | 权衡替代 | 见 4 节；父块是失败后替代 |
| 多样性 / 去重 | 近重复哈希 + 每书 5 条 | 已采用等价 | 针对同书/中英版本的重复 |
| 目录召回 | `toc_recall`：章节路径词项重叠 | 超出教程 | 框架类问题的章节定位 |
| 图谱召回 | `seed_concepts` / `select_new_seeds` / `load_mechanism_candidates` / `load_taxonomy_candidates` / `expand` / `plan_context` | 超出教程 | 条件化主张与生命周期环 |
| BarRef 召回 | `barref_lookup`：K线编号/图号 | 超出教程 | 解决“K线2/3构成什么”的结构化指代 |
| caption 定向召回 | `caption_search` + 图例目录 | 超出教程 | 只有明确要例子才触发 |
| 跨轮证据累积 | `acc` 字典跨轮保留 | 超出教程 | 修复“第二轮重复第一轮”的真实缺陷 |
| 失败隔离 | 每路 try/except + `failures` | 已采用且更强 | 单组件失败不拖垮全链路 |
| `visual_page` / `numeric` 路由 | QueryPlan 可输出，主链路未消费 | **接线缺口** | 与 metadata 一样，属于“计划字段未接线” |

**复盘**：检索主链路是教程第 4 章的直接落地：混合召回 → RRF → cross-encoder → 多样性 →
上下文扩展 → 充分性检查。PAknow 的增量主要在**多路专用召回**（toc/graph/barref/caption）
和**跨轮累积**。欠账也很清楚：压缩没做、metadata 过滤没接线、`visual_page`/`numeric`
是空路由。这三项都应在“下一步”里，而不是藏在“已实现”里。

## 8. 查询构建与改写

| All-in-RAG 方法 | PAknow 实现 | 判定 | 理由 |
|---|---|---|---|
| 提示工程直接改写 | `QueryPlan` 单次结构化调用 | 已采用 | 一次产出意图/改写/证据需求/路由 |
| 多查询分解 | `sub_queries` | 已采用 | 与教程一致 |
| Step-Back | `step_back` | 已采用 | 与教程一致 |
| HyDE | `hyde` | 已采用 | 与教程一致 |
| 查询路由（LLM 意图识别） | 单次 QueryPlan 分类 | 已采用 | 与教程一致 |
| 查询路由（嵌入相似性） | 未使用 | 权衡替代 | 少一次 LLM 调用，但路由粒度不如结构化计划 |
| 文本到元数据过滤器 | 未消费 | 接线缺口 | 见 7 节 |
| 文本到 Cypher / Text2SQL | 未使用 | 不适用/权衡替代 | 没有图数据库/结构化行情库 |
| 查询翻译成排序指令 | 未使用 | 不适用 | 当前没有“按字段排序”的检索源 |
| 术语消歧 | BTC/AI 语境偏置查询 | 超出教程 | 领域缩写歧义是真实问题 |
| 双语词典扩展 | 53 手工 + 180 标题对齐 = 233 概念 | 超出教程 | 中英资料跨语言召回 |
| 词边界保护 | ASCII 别名要求词边界 | 超出教程 | 修复 `TR` 命中 `trading`、`H2` 命中 `H20` |
| 查询集优先级 | `next_query > SOP > 原问题/改写`，去重取前 6 | 超出教程 | 修复补检索被截断的真实 bug |
| `wants_examples` 分流 | 明确要例子才走 caption/图例目录 | 超出教程 | 防止“检索到就罗列图例” |
| SOP 补充查询 | `sop_extra_queries` | 超出教程 | 程序性记忆参与查询构建 |
| 失败降级 | `route()` 异常 → 最简 QueryPlan | 已采用 | 保证系统可用 |

**复盘**：教程第 4 章的三种改写（Multi-query / Step-Back / HyDE）在 PAknow 里是**直接
照做**的，`router.py` 的 docstring 也明确写了参考来源。真正的差异是：PAknow 用**一次
结构化调用**替代“分类器 + 重写器 + 路由器”的多步链，并额外做了术语消歧与查询优先级。
代价是提示词更长、字段更多；收益是延迟和计划漂移更可控。

## 9. 生成

| All-in-RAG 方法 | PAknow 实现 | 判定 | 理由 |
|---|---|---|---|
| OutputParsers / PydanticOutputParser | `ModelGateway.chat_json` + Pydantic `model_validate` | 已采用等价 | 用 `response_format=json_object` + schema prompt |
| LlamaIndex 结构化输出 / Pydantic Programs | 未使用 | 不适用 | 没有用 LlamaIndex |
| 纯提示词 JSON + few-shot | `chat_json` 的 schema 注入；失败重试一次 | 已采用 | 与教程 2.3 思路一致 |
| GBNF / 语法约束 | 未使用 | 不适用/未实现 | 使用云端模型，不做本地 grammar |
| Function Calling / Tool Calling | 未使用 | **未实现** | 当前没有需要模型调用工具的已上线能力 |
| 引用绑定 | `[n]` → `evidence_ids` → 书/章/图/页，后端渲染 | 已采用且更强 | 模型只返回下标，越界丢弃；页码由程序渲染 |
| 三种生成模式 | `extractive` / `synthesis` / `chart` | 超出教程 | 定义/规则 vs 机制/框架 vs 读图 |
| 上下文分层 | 目录/图谱/BarRef/视觉观察不可引用；证据可引用 | 超出教程 | 派生材料不冒充原文 |
| 分析优先 / 图例目录 | 默认分析；明确要例子才给图，`figure_ids` 只能从目录选 | 超出教程 | 修复模型编造图号的死循环 |
| 证据不足处理 | 提示词要求“资料未说明”；`uncertainty` 字段 | 已采用且更强 | 不编造胜率/页码 |
| 流式输出 | 未实现 | **未实现** | 端到端 20–60s，流式是体验欠账 |
| 失败重试 | `_create` 一次重试 | 部分采用 | 只覆盖网络/瞬时错误，不覆盖结构错误 |

**复盘**：生成层的“结构化输出”与教程等价，但 PAknow 把**引用语义**做得更硬：模型输出
的是 `cited_indices`，后端映射到 `evidence_id`，越界直接丢弃。这一层不是提示词技巧，
而是数据契约。真正没做的是 Function Calling：当前能力里没有“让模型查行情/执行 SQL”
的已上线工具，硬加只会制造不可控路径。

## 10. 评估

| All-in-RAG 方法 | PAknow 实现 | 判定 | 理由 |
|---|---|---|---|
| RAG Triad：Context Relevance / Faithfulness / Answer Relevance | 未计算 | **未实现** | 三项都需要 judge 或标注；当前先用确定性硬约束 |
| 检索指标 Precision@k / Recall@k / F1 / MRR / MAP | 用"期望证据命中率"替代：并集探针 + 生产同口径两套 | 权衡替代 | 书目没有相关文档标注；期望证据点更接近领域需求 |
| LLM-as-judge | 盲评 `scripts/eval_judge.py`：三档分数 + 无支撑主张；只产出诊断，不覆盖确定性结论 | 已采用（受限） | 与"模型不自评当裁判"不冲突：它不参与通过与否 |
| ROUGE / BLEU / METEOR | 未使用 | 未实现 | 生成是归纳式回答，n-gram 重叠意义有限 |
| LlamaIndex Evaluation | 未使用 | 不适用 | 没有用 LlamaIndex |
| Ragas（Faithfulness / AnswerRelevancy / ContextPrecision / ContextRecall） | 未使用 | **未实现** | 与自建口径重叠，接入前先要有回归门槛 |
| Phoenix / OpenTelemetry 追踪 | 未使用；`RunTrace` 记录请求/路由/证据/失败/耗时/版本快照 | 部分替代 | 有可观测性，没有可视化/漂移分析 |
| 金标准数据集 | 3 个冻结集：20 题（v1）、60 题 4 层（v2）、10 张图（chart）；均带 SHA-256 | 已采用 | v2 覆盖检索 + 答案两层，chart 集只做确定性判定 |
| 回归门槛 / 消融 B0–B7 | 召回宽度 14/20 × 文本/图谱 2×2 消融；答案双臂；图表价位分桶 | 部分采用 | 有 harness 与数字；CI 只守确定性一致性（`tests/test_eval_artifacts.py`），生产链无门槛；重排/toc/邻接仍无消融 |
| 程序化 trace 信号 | `retrieval_error/no_evidence/low_evidence/no_answer` 按意图分组 | 已采用（局部） | 只服务 SOP 蒸馏，不是质量评测 |
| SOP 对照回放 | 未实现；`record_replay` 有接口，无数据 | **接线缺口** | `active` 晋升因此关闭 |

**复盘**：教程第 6 章给的评估闭环（指标 → 门槛 → 回归）这里只完成了第一段。现在能回答
"图谱扩展有没有用"（同宽度 +14 条期望证据）、"答案有没有变差"（配对 10 好 / 6 差 / 44 平）、
"读图准不准"（miss 0.875，2 题），但**没有一条数字能阻止代码合入**。更关键的是分层结果：
20 道机制题的覆盖率 ≈0——沿边扩展补得了概念，补不了论证链。这不是评测缺口，是检索设计
缺口，评测只是把它照出来了。细节见 [评测](chapter14-evaluation.md)。

## 11. 图谱 / GraphRAG

| All-in-RAG 方法 | PAknow 实现 | 判定 | 理由 |
|---|---|---|---|
| KG 构建：LLM/IE 抽取实体关系 | 对图注/图文关联的高价值子集抽取概念/主张/边 | 已采用（有界） | 全量 91k 单元成本高；先覆盖图注与解释 |
| 实体对齐 / 别名归一 | `terms.yaml` 词典桥接 + `concept_aliases` 同步 | 部分采用 | 没有自动实体解析/去重 |
| 质量控制 / 人工抽检 / 冲突消解 | `extraction_status=pending`；未做人工审核 | **未实现** | 候选主张可用但未审 |
| Neo4j / 属性图 | 未使用；SQLite 表 + Python 有界 BFS | 权衡替代 | 当前 1,105 节点 / 3,753 边，专用图库运维收益低 |
| Cypher / Text2Cypher | 未使用 | 不适用/权衡替代 | 查询是固定模式，不需要生成 Cypher |
| 多跳 / 子图 / 路径检索 | 概念分类 1 跳、机制链 2 跳，回取证据 ≤14 | 部分采用 | 有界，避免“趋势/市场”高出度节点爆炸 |
| GraphRAG（Microsoft）社区检测/Leiden/全局摘要 | 未实现 | **未实现** | 全局综述目前靠 toc + synthesis prompt |
| LightRAG 双层检索 | 词典种子 + 主张/证据扩展是轻量双层 | 部分采用 | 没有 LightRAG 的完整图索引 |
| FRAG 查询分流 | QueryPlan 路由 | 部分采用 | 固定路由，不是自适应复杂度分类 |
| GraphIRAG 迭代检索 | 充分性检查 + 补检索 | 部分采用 | 有迭代，但不以“信息增益”为停止准则 |
| 图检索指标（Context Precision/Recall/Citation） | 金标准 top-20 并集命中 + 「图独有」口径 | 部分采用 | 只有召回类指标；无 precision 与 citation 指标 |
| 条件化主张 | 存储 `context` / `modality`（1,888/1,888）；渲染与扩展读 `conditions` / `exceptions`（库里 0 条） | **半接线** | 目标是不让条件句读成绝对规则，但消费端键名不匹配，当前只是透传 |
| 图-文回填 | 图谱证据进入独立的「图谱扩展」提示区块 | 已采用 | 图谱结果必须回到原文；该区块不可引用，不进入 `evidence_ids` |
| 图谱可重建性 | 每条边带 `EdgeOrigin`，重建只替换同一来源（标题语法 328 条），种子边不被冲掉 | 超出教程 | 重跑抽取是可预期操作，不能靠"重建一次就别再碰" |

**复盘**：PAknow 的图谱不是教程第 7/9 章的缩小版，而是**为条件化交易知识定制**的：
主张在库里带 `context` / `modality`（1,888/1,888），设计意图是把条件随主张带进上下文；
但消费端读的是 `conditions` / `exceptions`（0 条），所以当前只是**透传**，还没做到"按条件筛选"。
图谱不作为独立打分路由，而是在文本召回后沿概念分类
（1 跳）与「概念 → 主张 → `SUPPORTED_BY` 证据」（2 跳）做有界扩展，预算为概念 ≤8、
边 ≤32、回取证据 **≤14**、图块 ≤1,200 tokens。扩展结果标记 `provenance=graph`、
`non_citable=True`，只作理解辅助，引用仍只指向原文证据（ADR-0002/0003）。

代价是规模小、没有社区摘要、没有 Neo4j。全局综述目前靠目录 + `synthesis` 提示词，
不是 GraphRAG 的全局摘要；这是明确的未实现项。v2 金标准的结论比 v1 清楚：同宽度下
图谱扩展把期望证据命中从 41/123 拉到 55/123（+14），21 道题的期望证据只在图谱侧被召回；
但 60 题全部撞到"证据 ≤14"的截断，且机制题覆盖率仍 ≈0（见 [评测](chapter14-evaluation.md)）。
图谱有用，但只解决"概念在哪"，不解决"论证链怎么走"。

## 12. Agentic RAG

| All-in-RAG 方法 | PAknow 实现 | 判定 | 理由 |
|---|---|---|---|
| Plan → Retrieve → Critique → Refine | QueryPlan → 多路召回 → `check_sufficiency` → `next_query` 补检索 | 已采用（受控） | 与教程核心循环一致 |
| 自主工具选择（vector/web/KG/SQL） | 路由是 QueryPlan 的固定集合 | 权衡替代 | 任务边界固定；自由选工具增加不确定性 |
| 多 Agent 协作 | 未使用 | 权衡替代 | 单流程已够；多 Agent 的协调成本高于收益 |
| 最大迭代次数 | 默认 3 轮 | 已采用 | 防止无限循环 |
| 诊断式改写 | `next_query` 来自缺失证据，不是盲改写 | 已采用 | 与教程一致 |
| 充分性评估 | LLM 结构化 `Sufficiency`；失败 fail-open | 已采用 | 没有核验器就退化为固定轮数 |
| 工具生态 | hybrid/toc/graph/barref/caption；无 web/SQL/numeric | 部分采用 | 与当前数据源一致 |
| Agent + KG 协同 | 图谱扩展（召回后沿概念/主张边补证据）+ `graph_evidence` | 部分采用 | 图不是独立路由、不参与打分；没有动态工具编排 |
| 轨迹记录 | `RunTrace`（请求/路由/证据/失败/耗时/版本） | 已采用且更强 | 可审计，不只是日志 |
| 检索历史记忆 | 单次运行 `acc`；跨运行只在 trace | 部分采用 | 没有长期“哪条路线成功”的记忆 |

**复盘**：Agentic 是“受控采用”，不是“完全没做”。教程的 Critique/Refine 被保留，
自主工具选择被去掉。这个取舍的理由是：当前只有四类证据源，且每一类都有明确的触发
条件；让模型自由选择不会带来新信息，只会让同一问题两次运行走不同路径。代价是遇到
新数据源时要改代码，而不是加一个 tool description。

## 13. 记忆与工程化

教程没有 SOP 生命周期；这一节主要是 PAknow 的自有设计，只列与教程工程章节的交叉点。

| 主题 | All-in-RAG | PAknow | 判定 |
|---|---|---|---|
| 运行轨迹 | Agentic 章节有 `retrieval_history` | `RunTrace` + 版本快照 + 失败记录 | 已采用且更强 |
| 长期记忆 | 未覆盖 | SOP 生命周期 + git 版本 | 超出教程 |
| 记忆晋升 | 未覆盖 | `candidate → replay_validated → shadow → active` | 部分实现；`active` 被评测卡住 |
| 自动优化 | 未覆盖 | 评价→蒸馏→发布，自动到 `shadow` | 已实现（局部） |
| 用户事实记忆 | 未覆盖 | `UserFactRow` 表存在，主链路未使用 | 接线缺口 |
| 作业/重试 | 未覆盖 | `ImportJobRow` 契约存在，主链路未使用 | 接线缺口 |
| API | 项目实战是 CLI | FastAPI 12 个路由：`/query` `/search` `/charts` `/evidence` `/documents` `/sources` `/figures` `/runs` `/sops` `/health` | 已采用且更强 |
| 多轮对话 | 未覆盖 | `QueryRequest.history`（最近 8 轮）+ 独立问题改写，返回 `query_used` / `rewritten` | 超出教程 |
| 外部集成 | 未覆盖 | MCP 适配器：11 个工具走 HTTP，不依赖内部包 | 超出教程 |
| 查看器 | 无 | 单页查看器 + PDF 页渲染 + 引用跳转 | 超出教程 |
| 截点遮罩 | 未覆盖 | `/charts?cutoff=` 右侧遮罩，模型看不到未来 K 线 | 超出教程 |
| 索引缓存 | FAISS 本地缓存 | Qdrant 持久化；无启动缓存层 | 等价 |
| 流式输出 | 有流式示例 | 未实现 | **未实现** |
| 查询缓存 | 未覆盖 | 未实现 | 未实现 |
| 鉴权/多用户 | 未覆盖 | 未实现（单用户本地） | 不适用 |
| 容器化/Postgres | Docker/Milvus | SQLite + Qdrant 嵌入式；可切 Postgres/server | 权衡替代 |
| 文档站 | MkDocs/Docsify | MkDocs Material + 本对照章 | 已采用 |

**复盘**：SOP 是 PAknow 相对教程的“加法”，但加法也有欠账：`UserFactRow`、`ImportJobRow`
两张表没有接入主链路；`record_replay` 有接口但没有评测数据；`active` 晋升关闭。工程层
最强的部分是**可审计性**（trace + 版本快照 + 引用定位），最弱的是**体验与规模化**
（无流式、无缓存、无鉴权、无 Docker 部署）。

## 14. 哪些是“没想到”，哪些是“权衡”

### 14.1 真正的“没想到 / 没落地”

| 项 | 状态 | 影响 |
|---|---|---|
| 上下文压缩（LLMChainExtractor/Filter） | 未实现 | 证据窗口靠截断，长文档可能带噪 |
| 元数据过滤器（`QueryPlan.metadata`） | 生成但未消费 | “只要某本书/某章”的问题无法精确过滤 |
| `visual_page` / `numeric` 路由 | 生成但未消费 | 路由字段是“装饰性”的，数值/页面问题没有专门路径 |
| HNSW/量化/索引参数基准 | 未做 | 不知道当前索引的召回/延迟上限 |
| Function Calling / 工具调用 | 未实现 | 模型不能执行数值/外部工具 |
| 流式输出 / 查询缓存 | 未实现 | 体验与并发成本 |
| 表格抽取 | 未实现 | 扩语料到表格类资料会漏 |
| 用户事实/作业表接线 | 未实现 | 表存在但不参与业务 |

### 14.2 有理由的“权衡替代”

| 项 | 替代方案 | 理由 |
|---|---|---|
| 固定/递归/语义分块 | 结构单元 + 句末切分 + 邻接 | 解析器已给语义边界；切分条件句风险高 |
| 父子块 | 阅读顺序邻接 | 父块实测 1,670/1,670 悬空；邻接覆盖 91,235 条 |
| ColPali/ColQwen2 | Qwen2.5-VL 观察式 | Blackwell CUDA 崩溃 / transformers 4.x 依赖 |
| Milvus/FAISS/Chroma | Qdrant 嵌入式 | 无 Docker；命名向量 + 过滤 + 可切 server |
| Neo4j/Cypher | SQLite 图表 + 有界 BFS | 1,105 节点规模；固定检索模式 |
| LLM 重排 / ColBERT | cross-encoder | 延迟/成本；late-interaction 索引不划算 |
| 多步 Agent / 多 Agent | 单次 QueryPlan + 受控补检索 | 固定数据源；稳定性/延迟优先 |
| GraphRAG 社区摘要 | toc + synthesis | 全局问题量少；社区检测成本高 |
| Text2SQL | 暂缓 | 无行情库；回测边界外 |
| 微调嵌入/生成 | 预训练 + 提示词 | 无标注对；版权/算力 |

### 14.3 明确“不该做”的

- **不让模型自评当裁判**：SOP 晋升必须有对照回放；自动闭环最多到 `shadow`。
- **不把派生材料当权威原文**：视觉观察、图谱主张、SOP 都不能替代原文引用。
- **不做 image-to-image**：当前只有“文本描述找图”，没有“以图搜图”；文档必须写明。
- **不做无数据支撑的数值**：没有行情数据就不进入 numeric 分支。

## 15. 下一步优先级

按“能否解锁已验证能力”排序，而不是按教程章节顺序：

| 优先级 | 事项 | 为什么现在做 |
|---|---|---|
| P0 | 生产链评测门槛（让检索+生成也进 CI） | 确定性一致性已进 CI；生产链仍需要一台能跑索引与模型的 runner |
| P0 | SOP 对照回放 + 五个操作接线 | 回放是 `active` 晋升的唯一证据；不接线则 SOP 只剩两个操作有效 |
| P0 | 接线 `QueryPlan.metadata` 到 Qdrant filter；实现或删除 `visual_page`/`numeric` | 消除“计划字段装饰化”，让路由输出可信 |
| P1 | 机制题检索（20 题覆盖率 ≈0） | 这是分层评测暴露出的设计缺口，不是调参 |
| P1 | 上下文压缩 + 证据窗口消融 | 当前靠 14 条截断，长文档可能带噪 |
| P1 | HNSW/量化/索引参数基准 | 8.1 万点已超嵌入式 Qdrant 建议值 4 倍，需要数据决定是否切 server |
| P1 | 流式输出 + 查询缓存 | 单轮 20–60 s 的主要体验瓶颈 |
| P2 | 表格抽取；Docling/MinerU 同页对照 | 只在扩语料时做，避免为 7 本书过度工程 |
| P2 | GraphRAG 社区摘要 / LightRAG 对照 | 只在全局综述类问题有稳定需求时做 |
| P2 | Function Calling / 数值工具 | 只有接入可信行情数据后才做 |

## 16. 一句话总结

> 教程回答“通用 RAG 应该有哪些零件”；PAknow 回答“当语料是 7 本图文混排、
> 条件密集、需要逐项引用的交易书时，哪些零件必须换、哪些可以少、哪些不能假装有”。
>
> 当前系统的强项是**证据可追溯、派生材料边界、条件化图谱、SOP 可回滚**；
> 评测已从"没有"走到"有数字"，但还没变成门槛；真正的欠账是**机制题检索**、
> **SOP 回放**，以及元数据过滤/压缩/索引调优这三个工程项。
