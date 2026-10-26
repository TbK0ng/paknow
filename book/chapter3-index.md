---
description: "BGE-M3 dense + 自研 TF 稀疏的索引设计、领域分词与词边界事故、嵌入式 Qdrant 的容量边界与重索引策略。"
---

# 3. 索引构建

> 索引决定"能不能被找到"。本章讲嵌入、稀疏分词、向量库、索引范围与重索引策略。

## 1. 嵌入模型：为什么是 BGE-M3 单模型

| 方案 | dense | sparse | 多语言 | 结论 |
|---|---|---|---|---|
| BGE-M3 | ✅ 1024 维 | 不用其 sparse：稀疏侧是自研 TF 向量 | 中英多语（厂商口径：支持 100+ 语言，未在本项目实测） | ✅ 采用（只用 dense） |
| bge-large-zh + BM25 | ✅ | 需另建 | ❌ / 分语言 | 两套模型两套维护 |
| E5-large | ✅ | ❌ | 一般 | 中文领域词弱 |
| OpenAI embedding | ✅ | ❌ | ✅ | 离线不可用、按量计费 |

**为什么稀疏侧要自己做**：BGE-M3 的 sparse 输出走它自己的词表，喂不进本项目的领域词。这里用的是自研 TF 向量（`paknow/retrieval/tokenize.py:53-73`：FNV-1a 哈希到 24-bit 桶，无 IDF、无长度归一化），因为本项目有大量**领域缩写**（H2、L2、BTC、STC、MTR、DBBF）和**图号**（图4.5、EMA20）。这些 token 如果被切碎，稀疏检索就失去精度。

分词器（`paknow/retrieval/tokenize.py`）的结构：

1. **领域模式保护**：`EMA\d+`、`H\d+`、`L\d+`、`图\s*\d+(\.\d+)?`、`BTC`、`STC` 等整词保留；
2. **词典 canonical 注入**：命中 `terms.yaml` 别名时，额外写入 canonical token（跨语言桥接）；
3. **jieba 中文切分**（词典别名加入 userdict）；
4. **英文词**。

### 1.1 词边界事故：TR 命中 trading

旧实现用 `if alias.lower() in text.lower()` 做子串匹配。后果：

- `TR`（trading range 缩写）命中所有含 "tr" 的英文词（**trading** 本身也是）；
- `H2` 命中 `H20`；
- `AI` 命中 `again`、`main`。

修复：ASCII 别名要求**词边界**（前后不是字母/数字），中文别名仍可子串。这个修复同时作用于查询扩展与稀疏分词。

### 1.2 稀疏索引必须重索引

词边界修复后，**查询侧**的分词变了，但**索引侧**仍是旧向量——那些被污染写入的 canonical token 还留在 Qdrant 里。另外，新增的 180 个双语术语也从未进入索引。

解决方案（`scripts/reindex_sparse.py`）：

- 只重算 **sparse** 向量，dense 不动；
- 用 Qdrant 的 `update_vectors`（命名向量，支持只更新指定向量）；
- 覆盖全部非页脚单元（约 8.1 万点）。

**为什么不全量重嵌入**：dense 向量只取决于文本与模型，文本没变就不必重算。全量重嵌入会白白消耗 GPU 时间与风险。

## 2. 向量库：为什么 Qdrant 嵌入式

| 方案 | 命名向量 | payload 过滤 | 离线 | 切换生产 | 结论 |
|---|---|---|---|---|---|
| Qdrant 嵌入式 | ✅ dense+sparse 同点 | ✅ | ✅ | 只改 URL | ✅ 采用 |
| FAISS | ❌ 需自行组合 | ❌ | ✅ | 需重写 | ❌ |
| Milvus | ✅ | ✅ | ❌ 需 Docker | ✅ | ❌ 环境不可行 |
| pgvector | ✅ | ✅ | 需 Postgres | ✅ | ❌ 需 Postgres |

**为什么命名向量重要**：混合检索要在**同一个点**上同时查 dense 与 sparse，再用 RRF 融合。Qdrant 的 `prefetch + FusionQuery(RRF)` 一次请求完成；如果两个索引分开，就要自己对齐 ID 与分数。

**离线模式的两个限制**：

1. **单进程只有一个 client**：嵌入式 Qdrant 同一个 path 只允许一个实例；除 `lru_cache` 单例外，`get_client`（`paknow/retrieval/qdrant_store.py`）还在 `<objects>/qdrant.lock` 上加进程级独占文件锁。第二个进程立即抛 `StoreLockedError`（带持锁 pid、时间与命令）；API 在启动自检（`_lifespan`）里就调 `check_store_ready`，所以第二个 API 进程**带着可读的原因拒绝启动**——不会出现"进程活着但只回 200 空命中"。
2. **规模超过官方建议值，且没测过**：客户端自己会打警告原文——"Local mode is not recommended for collections with more than 20,000 points. Collection <evidence_text> contains 81809 points."（`data/link_log.txt:1`）。`evidence_text` 实有 81,809 点，约为建议上限的 **4 倍**；本项目**没有做过**该规模下的延迟/内存基准，所以不能宣称"能跑"就等于"性能可接受"。

**切 server 的开关**：`settings.qdrant_url` 非空即走远程（`paknow/retrieval/qdrant_store.py:134-135`），环境变量 `QDRANT_URL`，配套 `docker-compose.yml` 里已有 `qdrant/qdrant:v1.12.4`（与 postgres 16 同文件）。默认值 `""` 表示嵌入式（`paknow/config.py:27`）。切 server 不看这把锁，也能解掉"单 client"限制，但**换不来已验证的性能结论**——那需要一次基准，不在这份文档里。

## 3. 索引范围与 payload

| 单元类型 | 是否索引 | 理由 |
|---|---|---|
| paragraph / heading / caption | ✅ | 主要内容 |
| footer | ❌ | 版式噪声（9,426 条） |
| 父块（`child_ids` 非空，743 条） | ❌ | 正文与子块重复，只在命中子块后回查；索引它会让同一段被召回两次（`paknow/retrieval/index.py` 的行过滤） |
| 观察单元（caption, derived） | ✅ | 视觉召回依赖它 |
| TOC | ❌ | 目录召回走数据库派生索引，不占向量库 |

payload 字段：`evidence_id / doc_id / block_type / text / figure_ids / chapter_path / pdf_page / figure_label / epub_href`。

**为什么不把整条 locator 塞进 payload**：payload 越大，检索时传输越慢；只需要过滤与展示字段即可，完整定位查 SQLite。

## 4. 索引一致性检查清单

一个 RAG 系统最常见的隐性故障是"索引与数据不一致"。本项目实际遇到并通过脚本修复的：

| 不一致 | 检测方式 | 修复 |
|---|---|---|
| 观察单元存了提示词 | 按文本匹配计数（当时 725 条） | 剥离 + 重嵌入 |
| 稀疏向量含子串污染 | 分词器变更后人工审计 | 全量 sparse 重索引 |
| 单元 `figure_ids` 为空 | 逐单元判空：`link_captions_to_figures.py` 把 caption/paragraph 全量拉到内存里按 `locator.bbox` 过滤（不是一条 SQL）；只有"取非空"那一步用到 SQL，在 `scripts/backfill_figure_links.py:22` 的 `func.json_array_length(...) > 0` | 图文链接回填 + payload 同步 |
| 邻接为空 | 邻接字段逐条判空（Python 侧读 `adjacent_ids`） | 阅读顺序回填（当时 81,809 条；现覆盖 91,978 条、非空 82,552） |
| 父块悬空 | 解析率 0 / 1,670 | 2026-10-25 回填 743 个父块（`scripts/backfill_parent_chunks.py`），解析率 1,670 / 1,670；`chunk_units` 改为保留父块 |

> **经验**：每次改分词器、词典、链接规则，都要问一句"索引里的旧数据还一致吗"。另外注意上一行那类"检测方式"——库里存的是 JSON 数组，空数组和有元素在 SQL 里长得一样容易骗人，真正的判空是在 Python 里把 JSON 解析出来数的。

## 5. 本章小结

- **dense 用单模型、稀疏侧自己分词**减少维护面，但领域词必须自己处理（BGE-M3 的 sparse 输出用不上）。
- 索引不是"建一次就完"：分词/词典/链接的每次变更都要求对应的重索引策略。
- 选择嵌入式 Qdrant 的核心收益是**命名向量 + 过滤 + 可切 server** 三件事同时满足；但 81,809 点已超官方建议上限 4 倍，切 server 是"保留退路"，不是"当前规模已被证明没问题"。
