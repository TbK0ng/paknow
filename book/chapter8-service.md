# 8. 服务与工程化

> 一个 RAG 系统能不能用，最后取决于工程细节：接口是否稳定、失败是否可见、部署是否可迁移。

## 1. 接口

| 接口 | 作用 | 关键设计 |
|---|---|---|
| `POST /query` | 问答 | 返回 `intent/routes/wants_examples/hits/figures/barrefs/answer` |
| `POST /charts` | 上传图表分析 | `cutoff` 参数遮罩未来数据 |
| `GET /evidence/{id}` | 单条证据 | 完整定位与文本 |
| `GET /sources/{doc_id}` | 来源信息 | 书、章节、页 |
| `GET /sources/{doc_id}/page/{page}/image` | PDF 页渲染 | 查看器点引用即可看原页 |
| `GET /runs/{run_id}` | 审计 trace | 失败、耗时、版本快照 |
| `GET /sops` | SOP 列表与版本 | 记忆审计 |
| `GET /` | 查看器 | 单页应用 |

**为什么 `figures` 只返回答案选中的图**：查看器不再成为"图库"，而是"答案配图"。理解类问题返回空是正确行为。

## 2. 存储

| 数据 | 默认 | 生产 |
|---|---|---|
| 元数据/证据/图谱/记忆 | SQLite `data/paknow.db` | Postgres（URL 可配，schema 无 SQLite 专有类型） |
| 向量 | Qdrant 嵌入式 `data/objects/qdrant` | Qdrant server（`QDRANT_URL`） |
| 图/页渲染等派生对象 | `data/objects/`（内容寻址） | 对象存储 |

**为什么默认嵌入式**：开发环境没有 Docker/外网；嵌入式零启动成本。切换生产只改环境变量，不改代码。

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
| 路由 LLM 调用 | 1 次 |
| 混合召回 | 6 条查询 × 2 路 |
| 重排 | 本地 GPU，首载 0.24s |
| 证据核验 | 0–3 次 LLM 调用 |
| 生成 | 1 次 LLM 调用 |
| 首次查询 | 额外包含 BGE-M3 加载 |

**优化方向（未做）**：路由与核验可并行；简单问题可跳过重排；生成流式返回。

## 6. 部署与文档站

```bash
# 服务
.venv/Scripts/python.exe -m uvicorn apps.api.main:app --port 8000

# 文档站（本地预览）
.venv/Scripts/python.exe -m mkdocs serve
# 构建静态站
.venv/Scripts/python.exe -m mkdocs build
# 发布到 GitHub Pages（需要已配置仓库 remote）
.venv/Scripts/python.exe -m mkdocs gh-deploy
```

文档目录为 `book/`，站点配置在仓库根 `mkdocs.yml`。

## 7. 本章小结

- 工程化的目标是"可迁移、可观测、可回滚"：接口字段稳定、失败写入 trace、存储可切换。
- 截点控制是图表分析正确性的底线，宁可保守（遮罩比例）也不要假装精确。
- 文档站与代码同仓库，避免"实现与说明脱节"。
