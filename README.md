# PAknow

多模态检索、证据图谱与可验证 SOP 记忆系统。对本地 PDF/EPUB 价格行为资料做
**可追溯出处**的知识问答、上传图表分析，并保留程序性记忆（SOP）的生命周期。

> 实现依据 `plan.md`。回测/行情数据与人工金标准评测按实施决策暂缓（见
> `plan.md` §0 实施决策记录）。

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
   → 索引(BGE-M3 dense+sparse / 目录 / ColPali 视觉)  → 多路召回+RRF融合
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

## 快速开始

```bash
# 1. 依赖（Windows 上 torch/torchvision 必须用 CUDA index，PyPI 默认是 CPU 版）
uv venv .venv
uv pip install --python .venv --index-url https://download.pytorch.org/whl/cu128 "torch==2.9.1" "torchvision==0.24.1"
uv pip install --python .venv -e ".[dev]"

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
- `GET /runs/{run_id}` — 审计 trace；`GET /sops` — SOP 版本

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

## 未完成 / 待补（见 plan.md §0）

- 真实行情数据 + 回测（P5，暂缓）
- 人工金标准评测 + 消融（B0–B7，暂缓）
- BPA 页面级视觉观察（当前覆盖 866 个图资产；BPA 整页图表可按需扩展 `index_visual.py`）
- 图谱抽取已对高价值子集（1007 单元 → 1252 节点/954 主张/954 边）完成；全量可继续

## 视觉方案说明（Blackwell 适配）

ColPali（PaliGemma）在本机 **RTX 5060 Ti（Blackwell sm_120）+ torch 2.9.1** 上触发
CUDA 内核越界崩溃；ColQwen2 checkpoint 又依赖 colpali_engine（要求 transformers 4.x）。
故视觉路由改用 **Qwen2.5-VL-3B**（Blackwell 兼容、transformers 5.x 原生）：对图资产生成
结构化观察，观察文本作为**派生索引材料**（非权威来源）进入同一 dense+sparse 索引。
上传图表分析走 cloud vision（deepseek-flash）。
