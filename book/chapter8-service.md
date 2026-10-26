---
description: "12 个 API 路由、多轮对话契约、MCP 适配器、存储选型、截点遮罩、性能观察与 CI 部署。"
---

# 8. 服务与工程化

> 一个 RAG 系统能不能用，最后取决于工程细节：接口是否稳定、失败是否可见、部署是否可迁移。

## 1. 接口

**12 个路由，全部在 `apps/api/main.py`**（括号内为该路由装饰器所在行）：

| 路由 | 作用 | 关键设计 |
|---|---|---|
| `GET /health` (`:160`) | 存活探针 | 只回 `{"status": "ok"}`，不碰库、不碰模型 |
| `POST /query` (`:165`) | 问答 | 支持多轮 `history`；返回引用、图、图谱扩展 |
| `POST /search` (`:174`) | 只检索不生成 | 不路由、不生成，agent 可低成本查证据；支持 `doc_id` / `block_type` 过滤 |
| `GET /documents` (`:199`) | 语料清单 | 书名、作者、译者、页数、条目数 |
| `POST /charts` (`:219`) | 上传图表分析 | `cutoff` 参数遮罩未来数据；额外返回 `chart_read` |
| `GET /evidence/{id}` (`:239`) | 单条证据 | 完整文本 + 定位 + 关联图 |
| `GET /sources/{doc_id}` (`:269`) | 来源信息 | 书、章节、页、证据数、图数 |
| `GET /runs/{run_id}` (`:291`) | 审计 trace | 意图、路由、证据、失败、耗时 |
| `GET /sops` (`:308`) | SOP 列表与版本 | 记忆审计 |
| `GET /` (`:313`) | 查看器 | 单页应用（`include_in_schema=False`） |
| `GET /figures/{figure_id}/image` (`:318`) | 图资产 | 让远程 agent 客户端直接取图，不必自己解析本地路径 |
| `GET /sources/{doc_id}/page/{page}/image` (`:334`) | PDF 页渲染 | 查看器点引用即可看原页 |

**启动自检**（`apps/api/main.py:29` 的 `_lifespan`，`paknow/retrieval/qdrant_store.py` 的 `check_store_ready`）：进程起来之前先只读地确认 `evidence_text` 集合存在且可计数。三种情况各有独立的可读原因，**服务拒绝启动**（uvicorn 报 `Application startup failed` 并退出），不做"活着但答案永远为空"的半死服务：

| 情况 | `reason` | 报错（原文） |
|---|---|---|
| 集合不存在（没建库） | `missing` | `向量库集合 evidence_text 不存在（…/qdrant）：尚未建库，先运行入库脚本。` |
| 集合存在但没有证据单元 | `empty` | `向量库集合 evidence_text 为空（…/qdrant）：入库未完成，入库完成后再启动服务。` |
| 被其它进程占用 | `locked` | `内嵌向量库已被占用（pid=…, cmd=…）；评测或脚本正在占用存储，请稍后重试。` |

两个细节决定了它是"自检"而不是"初始化"：**不走 `ensure_collections`**（那条路径会静默建集合，把没建库伪装成正常），并且**不创建任何集合**——`check_store_ready` 只调用 `collection_exists` 与 `count`。因此 `/health` 的语义是"能起来就是可用的"：进程只在存储就绪后才接受请求，不存在 `ok` 但答不出的状态。

### 1.1 `POST /query` 的真实契约

请求体是 `QueryRequest`：`query` + `history`（`ChatTurn{role: "user"|"assistant", content}`，默认空列表）+ `debug`（布尔，默认 `false`；显式置 `true` 才返回排障块）。

- 带 `history` 时，服务先做一次**独立问句改写**调用（`apps/api/main.py:79-118`，只取最后 `MAX_HISTORY_TURNS = 8` 轮），把"那它呢"补成可独立检索的问句；改写失败回退原问题，对话不报错，只是丢掉上下文；
- 因此这条路由比其他路由多两个响应字段：`query_used`（实际检索用的问句）与 `rewritten`（是否真的改写过）。

响应键完整清单（`apps/api/main.py:182-197` + `:210-211`，`debug` 块由 `:159-176` 组装）：

| 键 | 内容 |
|---|---|
| `run_id` | trace 锚点 |
| `intent` / `is_framework` | 路由结果 |
| `routes` | 实际用到的召回路径 |
| `answer` | 答案正文（**字符串**，不是分段结构） |
| `uncertainty` | 不确定性说明 |
| `citations` | `[n]` 解析后的出处 |
| `figures` | 答案实际选中的图 |
| `rounds` | 补检索轮次 |
| `graph_stats` | 图谱扩展规模 |
| `graph` | 逐条扩展证据（文本截断到 240 字） |
| `degraded` | `true` 表示这次答案是「本次生成失败…请重试」的占位，不是模型答案 |
| `degradation` | 降级原因 `{reason, detail}`；`reason` 与读图失败同一套分类 |
| `query_used` / `rewritten` | 仅当请求带 `history` 才有实际内容 |
| `debug` | 仅当请求带 `debug: true` 时才出现；见下 |

`debug` 块只读、不额外落库，字段：`hits`（每条只留 `evidence_id` / `score` / `block_type`，不含正文）、`citable_ids`（与生成侧同一份引用绑定，派生观察永不进入）、`context_unit_counts`（`total` / `parents` / `adjacent`）、`sufficiency`（核验器判定）、`failures`（结构化清单，每条 `{stage, category, detail}`；`category` 用 `timeout` / `invalid_structure` / `no_content` 三类，其余链路失败记 `error`，降级答案正因此可见）、`rounds`、`graph_stats`。

**接口默认不返回什么**：`hits`、`barrefs`、`wants_examples` 都只存在于内部 `run_query` 的返回 dict；只有显式 `debug: true` 时 `hits` 的 id/分数/块类型才出现在 `debug` 块里。要完整检索结果走 `POST /search`；`chart_read` 只挂在 `POST /charts` 上，`POST /query` 保持纯文本答案的形状。

**为什么 `figures` 只返回答案选中的图**：查看器不再成为"图库"，而是"答案配图"。理解类问题返回空是正确行为。

### 1.2 MCP 适配器（`integrations/paknow-mcp/`）

仓库自带一个 MCP 服务器，把上面的 HTTP 接口包成 **11 个工具**：`paknow_ask` / `paknow_chart` / `paknow_search` / `paknow_evidence` / `paknow_documents` / `paknow_source` / `paknow_page_image` / `paknow_figure_image` / `paknow_run` / `paknow_sops` / `paknow_health`。

- **不 import PAknow 内部**：适配器只走 HTTP。这不是洁癖——SQLite、嵌入式 Qdrant（一个索引路径只允许一个客户端）、本地模型都归 FastAPI 进程独占，第二个进程挤进去就会撞锁；
- **两种传输**：`paknow-mcp` 走 stdio（Pi / Claude Desktop / Cursor），`paknow-mcp --transport streamable-http --port 8765` 走 HTTP；
- **随包附带的技能**：`integrations/paknow-mcp/skills/paknow/SKILL.md` 规定"价格行为问题必须走 `paknow_ask`，服务不可用时不凭记忆回答"；
- **打包走集成自己的脚本**：`integrations/paknow-mcp/scripts/build_release.py` 生成自带 wheel 与 `skill/paknow/SKILL.md` 的发布包（只构建、不发布）；发布是 `release.py`，验证是 `verify_release.py`；
- **配置**：`PAKNOW_BASE_URL`（默认 `http://127.0.0.1:8000`）、`PAKNOW_TIMEOUT`（默认 300 秒；首次查询要加载本地模型）。

**为什么单独一个进程而不是塞进 API 进程**：MCP 客户端的生命周期由编辑器/Agent 管，API 进程要长驻且独占模型与索引；分开以后客户端重启不打断服务，服务升级也不用重装客户端。

## 2. 存储

| 数据 | 默认（`paknow/config.py:24-27`） | 生产 |
|---|---|---|
| 元数据/证据/图谱/记忆 | `PAKNOW_DB_URL=sqlite:///./data/paknow.db` | Postgres（URL 可配，schema 无 SQLite 专有类型） |
| 向量 | `QDRANT_URL` 留空 = 嵌入式 `data/objects/qdrant` | `QDRANT_URL=http://localhost:6333` |
| 图/页渲染等派生对象 | `data/objects/`（内容寻址） | 对象存储 |

**默认值的两个坑，先说清楚**：

- 嵌入式 Qdrant 的容量不是"理论上限"。本地库快照 2026-10-24 的 `evidence_text` 集合有 **81,809 points**，客户端一启动就打印 `Local mode is not recommended for collections with more than 20,000 points`（`data/link_log.txt:1`）——也就是说当前规模已经超出推荐值 4 倍，本地模式的性能与内存表现不再有任何保障；
- `page_visual` 集合是**空的**：页面视觉检索只有代码路径，没有数据。

**切服务端是唯一出路**：`docker-compose.yml` 起 `postgres:16-alpine` 与 `qdrant/qdrant:v1.12.4`，然后把 `PAKNOW_DB_URL`、`QDRANT_URL` 指过去。切换只改环境变量，不改代码——这是当初选这两个组件的主要理由。

**为什么默认仍然用嵌入式**：开发环境没有 Docker/外网；嵌入式零启动成本。把它写进默认值，是承认"这是单机开发配置"，而不是宣称它能承载生产规模。

## 3. 上传图表与截点控制

`POST /charts?cutoff=0.3`：

- 遮罩图片右侧 30%；
- 遮罩在进入模型之前完成，模型**看不到**截点之后的 K 线；
- 用于"模拟当时只能看到这些 K 线"的实时分析（防止未来信息泄漏）。

**为什么用宽度比例而不是 K 线根数**：系统没有解析上传图的坐标轴与 K 线间距，无法可靠地把"隐藏 N 根"换算成像素。比例是诚实且可控的近似；文档明确标注这个限制。

## 4. git 工程实践

| 规则 | 理由 |
|---|---|
| `*.pdf` / `*.epub` 不入库 | 版权 + 体积（单个 1.3GB 超 GitHub 单文件限制） |
| `data/*.db*` 不入库 | 派生数据，可重建 |
| `sop_registry/` 入库 | 记忆需要版本历史 |
| 每完成一个优化项一次提交 | 历史可读、可回滚 |
| 提交信息写"为什么" | 与本文档的设计叙事对应 |

## 5. 性能观察

| 环节 | 实测 |
|---|---|
| 单次问答总耗时 | 20–60s（云端生成占大头） |
| 路由 LLM 调用 | 1 次；带 `history` 时 2 次（1 次独立问句改写 + 1 次路由） |
| 混合召回 | 6 条查询 × 2 路 |
| 重排 | 本地 GPU 交叉编码器（`paknow/retrieval/pipeline.py:216-228`），首次调用含模型加载 |
| 证据核验 | 0–3 次 LLM 调用（受 `QueryPlan.max_search_rounds` 约束） |
| 生成 | 1 次 LLM 调用 |
| 首次查询 | 额外包含 BGE-M3 加载 |

**优化方向（未做）**：路由与核验可并行；简单问题可跳过重排；生成流式返回。

## 6. 部署与文档站

```bash
# 服务
.venv/Scripts/python.exe -m uvicorn apps.api.main:app --port 8000

# 文档站（本地预览）：必须换端口，默认的 8000 会和上面的 uvicorn 撞
.venv/Scripts/python.exe -m mkdocs serve --dev-addr 127.0.0.1:8080
# 构建静态站（`--strict` 会把坏链接/坏 nav 当错误）
.venv/Scripts/python.exe -m mkdocs build --strict
# 手动发布到 GitHub Pages（需要已配置仓库 remote）
.venv/Scripts/python.exe -m mkdocs gh-deploy --force
```

`mkdocs serve` 默认绑 `127.0.0.1:8000`，与本项目的 API 默认端口相同——不显式给 `--dev-addr` 就会出现"起得来但打不开"的假故障（端口被 uvicorn 占着），所以本地同时跑服务和文档站时固定用 8080。

文档目录为 `book/`，站点配置在仓库根 `mkdocs.yml`。正式发布走 CI：`.github/workflows/docs.yml` 在 `main` 上改动 `book/**` 或 `mkdocs.yml` 时自动 `mkdocs gh-deploy --force --clean --strict`，所以文档不会再出现"代码更新了、站点还停在旧版本"的静默漂移。

## 7. 本章小结

- 工程化的目标是"可迁移、可观测、可回滚"：接口字段稳定、失败写入 trace、存储可切换。
- 截点控制是图表分析正确性的底线，宁可保守（遮罩比例）也不要假装精确。
- 文档站与代码同仓库，避免"实现与说明脱节"。
