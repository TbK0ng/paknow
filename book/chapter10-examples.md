---
description: "从环境安装到全量摄入、视觉索引、图谱构建、问答与图表上传的真实命令与返回结构。"
---

# 运行示例

> 本章给出从零到可用的完整命令序列与真实输出。命令均在项目根目录、Windows + `.venv` 环境下验证。

## 1. 环境

```bash
uv venv .venv
# Windows 上 torch/torchvision 必须用 CUDA 索引（PyPI 默认是 CPU 版）
uv pip install --python .venv --index-url https://download.pytorch.org/whl/cu128 "torch==2.9.1" "torchvision==0.24.1"
uv pip install --python .venv -e ".[dev]"

# 配置
cp .env.example .env   # 填 DEEPSEEK_API_KEY；HF_HOME 默认 E:\use_model
```

**注意**：本地模式不需要动这两个变量。`paknow/config.py:28-34` 的默认值就是 SQLite + 内嵌 Qdrant；`.env.example` 里已经把两行基础设施 URL **注释掉**。不要写成 `PAKNOW_DB_URL=`（空串会覆盖默认值，SQLite 默认不会生效），要切服务端模式再取消注释并填 `postgresql+psycopg://…` 与 `http://localhost:6333`。

## 2. 离线构建

```bash
# 摄入脚本内置的 5 份书（3 EPUB 原生 + 2 PDF PyMuPDF）；
# 另 2 份（pabook 百科 8,659 页、Price Action 笔记）需单独注册，合计 7 份
.venv/Scripts/python.exe scripts/ingest_all.py

# 视觉观察索引（Qwen2.5-VL，927 图）
.venv/Scripts/python.exe scripts/index_visual.py

# 证据图谱：抽主张 → 补目录/对齐边 → 挂图
.venv/Scripts/python.exe scripts/extract_graph.py           # 图注/图关联文本 → 主张
.venv/Scripts/python.exe scripts/build_taxonomy.py --apply   # 标题层级边（PART_OF / TRANSITIONS_TO / INVALIDATED_BY / EXCEPTION_TO）
.venv/Scripts/python.exe scripts/align_claims.py --apply     # 主张主语对齐到规范概念（幂等）
.venv/Scripts/python.exe scripts/seed_and_extract.py         # 市场周期种子 + 两本补登记书重抽
.venv/Scripts/python.exe scripts/link_figures_to_graph.py    # 图资产挂到 ILLUSTRATED_BY

# 数据一致性维护（按需）
.venv/Scripts/python.exe scripts/backfill_adjacency.py          # 邻接回填
.venv/Scripts/python.exe scripts/backfill_figure_links.py       # 图↔文本反向链接
.venv/Scripts/python.exe scripts/link_captions_to_figures.py    # 图注几何/标签链接
.venv/Scripts/python.exe scripts/fix_observations.py            # 观察污染清洗
.venv/Scripts/python.exe scripts/reindex_sparse.py              # 稀疏向量重索引
.venv/Scripts/python.exe scripts/extract_bilingual_terms.py     # 双语术语提取
.venv/Scripts/python.exe scripts/sync_concept_aliases.py        # 概念别名同步
.venv/Scripts/python.exe scripts/extract_barrefs.py             # K线指代提取
```

构建完先跑两个端到端自检（不启动服务，直接走 `run_query`）：

```bash
.venv/Scripts/python.exe scripts/smoke_query.py    # 全链问答
.venv/Scripts/python.exe scripts/smoke_chart.py    # 图表上传链路
```

> 两个脚本已按当前返回结构对齐（`run_query` 返回单值 `intent`，答案在 `answer["answer"]` 里），直接可跑；它们不做断言，只打印链路结果，适合排查"哪一步没返回"。

## 3. 服务与问答

```bash
.venv/Scripts/python.exe -m uvicorn apps.api.main:app --port 8000
```

### 3.1 知识问答

```bash
curl -s -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"query":"市场周期通常是如何转换的？"}'
```

返回结构（字段齐全、数值仅示意，长数组已截断；`apps/api/main.py:182-197` 组装，`:210-211` 补两个改写字段）：

```json
{
  "run_id": "9f3c1d…",
  "intent": "synthesis",
  "is_framework": true,
  "routes": ["hybrid_text", "toc"],
  "answer": "## 概念骨架\n脉冲 → 紧密通道 → 宽松通道 → 交易区间 → 新脉冲 …",
  "uncertainty": [],
  "citations": [
    {
      "evidence_id": "ev_…",
      "citation": "《…》 → 第 2 章 … → PDF 第 … 页",
      "text": "…",
      "doc_id": "…"
    }
  ],
  "figures": [],
  "rounds": 1,
  "graph_stats": {
    "candidates": "…", "selected": 14, "tokens": "…",
    "truncated_edges": false, "truncated_evidence": true, "truncated_tokens": false,
    "seeds": 4, "accumulated": "…", "enabled": true
  },
  "graph": [
    {
      "evidence_id": "ev_…",
      "relation": "TRANSITIONS_TO",
      "from_label": "紧密通道",
      "to_label": "交易区间",
      "claim": "…",
      "concept": "紧密通道",
      "source": "heading_parse",
      "seen_index": null,
      "doc_id": "…",
      "chapter_path": ["第 2 章 …"],
      "pdf_page": "…",
      "printed_page": null,
      "figure_label": null,
      "text": "…（截断到 240 字）"
    }
  ],
  "query_used": "市场周期通常是如何转换的？",
  "rewritten": false,
  "degraded": false,
  "degradation": null
}
```

三个容易踩的点：

- `answer` 是**扁平字符串**（markdown），不是嵌套对象；`uncertainty` 与 `citations` 与它平级。
- 默认响应里**没有** `hits` / `barrefs` / `wants_examples`——它们是内部 `run_query` 的返回字段。想看原始证据用独立的 `POST /search`；排障时在请求里加 `debug: true`，响应会多一个只读 `debug` 块（命中的 id/分数/块类型、可引用 id、上下文计数、充分性判定、结构化失败清单、轮数、图谱统计），默认请求体尺寸不变。
- `graph` 里每条都是 `non_citable` 的理解辅助，不进 `[n]` 引用编号；`seen_index` 非空表示这条证据已作为可引用单元出现过，所以 `text` 为空。
- 生成失败不会 500：答案正文是「本次生成失败…请重试」的占位文字，同时 `degraded: true`、`degradation.reason` 给出失败分类（`timeout` / `invalid_structure` / `no_content`，与读图失败同一套词）。判断占位请看这两个字段，不要靠答案长度猜。

### 3.2 定义类问题

```bash
curl -s -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"query":"双底多头旗形是什么意思？如何判断？"}'
```

行为：路由判为 `intent=knowledge_qa`，`figures` 为空（分析优先，不主动罗列图例）；答案先给原文定义，再区分"双底回调（反转）"与"双底多头旗形（持续）"，最后给判断要点。

### 3.3 要图例

```bash
curl -s -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"query":"给我看双底多头旗形的图例"}'
```

行为：内部计划置 `wants_examples=true`（该字段只在 `run_query` 的返回值里，`POST /query` 不暴露）；检索阶段做 caption 定向召回，`figures` 只返回答案实际选中的真实图（例如图号 `8.19`），不是整个图例目录。

### 3.4 K 线指代

```bash
curl -s -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"query":"K线2和K线3构成什么形态？"}'
```

行为：内部 `barrefs` 拿到结构化指代（同属 `run_query` 内部字段，API 不返回）；答案据编号定位，并对图注冲突（同一组 K 线同时被标为"双底牛旗/双顶熊旗"）保持谨慎。

### 3.5 上传图表（含截点遮罩）

```bash
curl -s -X POST "http://localhost:8000/charts?query=当前形态接下来怎么看&cutoff=0.3" \
  -F "file=@chart.png"
```

`cutoff=0.3` 表示遮罩图片右侧 30%，模型看不到截点之后的 K 线。

### 3.6 记忆审计

```bash
curl -s http://localhost:8000/sops
# 或在仓库中查看版本历史
git log --oneline -- sop_registry/
```

手动触发一轮记忆优化：

```bash
.venv/Scripts/python.exe scripts/optimize_memory.py
```

## 4. 文档站

```bash
# mkdocs 默认也绑 127.0.0.1:8000，与上面的 uvicorn 撞端口，必须换一个
.venv/Scripts/python.exe -m mkdocs serve --dev-addr 127.0.0.1:8080   # http://127.0.0.1:8080
.venv/Scripts/python.exe -m mkdocs build                             # 输出到 site/
```

## 5. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `torch.cuda.is_available() == False` | 装了 PyPI 的 CPU 版 torch | 用 cu128 索引重装 |
| 首次查询很慢 | BGE-M3 / 重排模型加载 | 预热一次即可 |
| 图例为空 | 未做图文链接回填 | 运行 `link_captions_to_figures.py` + `backfill_figure_links.py` |
| 稀疏检索突然变差 | 改过分词器但未重索引 | 运行 `reindex_sparse.py` |
| `database is locked`（并发） | SQLite 写锁 | 降低并发或启用 WAL |
