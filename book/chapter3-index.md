# 3. 索引构建

> 索引决定"能不能被找到"。本章讲嵌入、稀疏分词、向量库、索引范围与重索引策略。

## 1. 嵌入模型：为什么是 BGE-M3 单模型

| 方案 | dense | sparse | 多语言 | 结论 |
|---|---|---|---|---|
| BGE-M3 | ✅ 1024 维 | ✅ 同模型产出 | 中英等 100+ 语言 | ✅ 采用 |
| bge-large-zh + BM25 | ✅ | 需另建 | ❌ / 分语言 | 两套模型两套维护 |
| E5-large | ✅ | ❌ | 一般 | 中文领域词弱 |
| OpenAI embedding | ✅ | ❌ | ✅ | 离线不可用、按量计费 |

**为什么稀疏侧要自己做分词**：BGE-M3 的 sparse 权重来自其内置词表，但本项目有大量**领域缩写**（H2、L2、BTC、STC、MTR、DBBF）和**图号**（图4.5、EMA20）。这些 token 如果被切碎，稀疏检索就失去精度。

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

**离线模式限制**：嵌入式 Qdrant 单进程只允许一个 client 实例（已用 `lru_cache` 单例控制）。生产切 server 时无此限制。

## 3. 索引范围与 payload

| 单元类型 | 是否索引 | 理由 |
|---|---|---|
| paragraph / heading / caption | ✅ | 主要内容 |
| footer | ❌ | 版式噪声（9,426 条） |
| 观察单元（caption, derived） | ✅ | 视觉召回依赖它 |
| TOC | ❌ | 目录召回走数据库派生索引，不占向量库 |

payload 字段：`evidence_id / doc_id / block_type / text / figure_ids / chapter_path / pdf_page / figure_label / epub_href`。

**为什么不把整条 locator 塞进 payload**：payload 越大，检索时传输越慢；只需要过滤与展示字段即可，完整定位查 SQLite。

## 4. 索引一致性检查清单

一个 RAG 系统最常见的隐性故障是"索引与数据不一致"。本项目实际遇到并通过脚本修复的：

| 不一致 | 检测方式 | 修复 |
|---|---|---|
| 观察单元存了提示词 | `text LIKE '%观察器%'` 计数 725 | 剥离 + 重嵌入 |
| 稀疏向量含子串污染 | 分词器变更后人工审计 | 全量 sparse 重索引 |
| 单元 `figure_ids` 为空 | `json_array_length > 0` 计数 | 图文链接回填 + payload 同步 |
| 邻接为空 | 邻接字段计数 0 | 阅读顺序回填 81,809 |
| 父块悬空 | 解析率 0/743 | 改为邻接方案 |

> **经验**：每次改分词器、词典、链接规则，都要问一句"索引里的旧数据还一致吗"。

## 5. 本章小结

- 单模型 dense+sparse 减少维护面，但稀疏侧必须自己处理领域词。
- 索引不是"建一次就完"：分词/词典/链接的每次变更都要求对应的重索引策略。
- 选择嵌入式 Qdrant 的核心收益是**命名向量 + 过滤 + 可切生产**三件事同时满足。
