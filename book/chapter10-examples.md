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

## 2. 离线构建

```bash
# 全量摄入（EPUB 原生 + PDF PyMuPDF）
.venv/Scripts/python.exe scripts/ingest_all.py

# 视觉观察索引（Qwen2.5-VL，927 图）
.venv/Scripts/python.exe scripts/index_visual.py

# 证据图谱
.venv/Scripts/python.exe scripts/extract_graph.py
.venv/Scripts/python.exe scripts/seed_and_extract.py
.venv/Scripts/python.exe scripts/link_figures_to_graph.py

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

返回结构（节选）：

```json
{
  "intent": "synthesis",
  "routes": ["hybrid_text", "toc"],
  "wants_examples": false,
  "hits": [ {"evidence_id": "...", "reranked": true, "...": "..."} ],
  "figures": [],
  "barrefs": [],
  "answer": {
    "answer": "## 概念骨架... 脉冲→紧密通道→宽松通道→交易区间→新脉冲 ...",
    "evidence_ids": ["..."],
    "figure_ids": [],
    "uncertainty": []
  }
}
```

### 3.2 定义类问题

```bash
curl -s -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"query":"双底多头旗形是什么意思？如何判断？"}'
```

行为：`intent=knowledge_qa`，`wants_examples=false`，`figures=[]`；答案先给原文定义，再区分"双底回调（反转）"与"双底多头旗形（持续）"，最后给判断要点。

### 3.3 要图例

```bash
curl -s -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"query":"给我看双底多头旗形的图例"}'
```

行为：`wants_examples=true`；检索阶段做 caption 定向召回，`figures` 返回答案实际选中的真实图（例如图号 `8.19`）。

### 3.4 K 线指代

```bash
curl -s -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"query":"K线2和K线3构成什么形态？"}'
```

行为：`barrefs` 返回结构化指代（10 条量级）；答案据编号定位，并对图注冲突（同一组 K 线同时被标为"双底牛旗/双顶熊旗"）保持谨慎。

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
.venv/Scripts/python.exe -m mkdocs serve      # http://127.0.0.1:8000
.venv/Scripts/python.exe -m mkdocs build      # 输出到 site/
```

## 5. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `torch.cuda.is_available() == False` | 装了 PyPI 的 CPU 版 torch | 用 cu128 索引重装 |
| 首次查询很慢 | BGE-M3 / 重排模型加载 | 预热一次即可 |
| 图例为空 | 未做图文链接回填 | 运行 `link_captions_to_figures.py` + `backfill_figure_links.py` |
| 稀疏检索突然变差 | 改过分词器但未重索引 | 运行 `reindex_sparse.py` |
| `database is locked`（并发） | SQLite 写锁 | 降低并发或启用 WAL |
