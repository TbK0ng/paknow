# 00: 图表读图契约（grill-with-docs 决议）

**What to build:** 决议记录，不是 spec。`to-spec` 应把下面 12 条当作已定输入，不再重开；spec 只负责把它们写成可执行的验收。

**Blocked by:** None (can start immediately)

**Status:** resolved

## 事实（已查证，spec 不用再验）

| 事实 | 出处 |
|---|---|
| 上传图目前直接 base64 进模型，无归一化；32 MB 截图会踩供应商上限并 500 | `paknow/gateway.py`（`_prepare_image`，已修于 66b194f） |
| `RunTraceRow.output` / `request` 是无类型 `JSON`，存结构化读图零迁移 | `paknow/db/models.py:192,197` |
| `save_trace(..., answer=...)` 原样落库，唯一通道是 `generate()` 的返回值 | `paknow/memory/trace.py:11-38`、`paknow/workflows/graph.py:186-200` |
| `GET /runs/{run_id}` 已整块返回 `output`，读图因此天然可审计 | `apps/api/main.py:260-274` |
| `/charts` 单文件；`run_query` 本身收 `images` 列表 | `apps/api/main.py:194-205`、`paknow/workflows/graph.py:240-247` |
| `_resolve_answer` 是字段白名单，新增 `chart_read` 必须显式加 | `apps/api/main.py:117-132` |
| 评测器 `eval_answers.run_item` 调 `run_query(question, expand_graph=…)`，**不传 images** —— 图表评测的唯一接缝 | `scripts/eval_answers.py:187` |
| 60 题文本集冻结（`version:2`、`frozen_at`、每题锁死 6 条 `expanded_queries`） | `evals/gold/graph_expansion_v2.json`、`tests/test_gold_dataset_v2.py` |
| 判定器分两段：确定性检查（引用合法性、`expected_evidence` 覆盖）→ 一次盲判 | `scripts/eval_judge.py` |

## 决议

| # | 决定 |
|---|---|
| 1 | 答案分两层：① 图上可读标注位原样全列（含 `1 @ 7526.75` 原文），② 再按资料口径收敛 5–8 个「建议关注」；第②层不得导致第①层删减 |
| 2 | 关键位优先级写死：`图上标注 > 前高/前低 > ONH/ONL > 区间边缘/平台 > 均线`；数值冲突保留更极端者，另一条进 `unreadable`；均线参数未知时不得给具体价位 |
| 3 | 评测用三个独立率：漏读 / 多读 / 漂移（价位出现但末位不一致）；命中要整条一致，漂移单独计，**不设容差** |
| 4 | 本 spec 只做：两阶段读图 + 结构化产物出到 API/页面 + 图表 golden 评测 + 条带定位。不换视觉模型、不做局部放大交互、不做多图上传、不动 `cutoff` |
| 5 | 延迟：图表问题 p50 ≤ 120s；超时降级为单阶段 `mode="chart"`，页面标明「未结构化读图」 |
| 6 | 切条带时只给条带序号（提示词声明「共 N 张，从上到下为条带 1..N」），`swings/levels` 带 `strip` 索引；不在像素上烧字 |
| 7 | 图表 golden 独立成 `evals/gold/chart_v1.json`；只存 `image_sha256` + 相对路径，图片不进 git，缺图判 skipped；判定只跑确定性段，不盲判 |
| 8 | `chart_read` 全量落轨迹（`levels` 全部标注位 + `annotations` 原文 + `unreadable`），单条 < 2KB |
| 9 | 正文仍由模型散文写（已验证可读）；结构化 `chart_read` 另作页面可折叠「读图数据」面板 + `/runs/{id}` 审计。两份出自同一次调用，不额外加调用 |
| 10 | 非行情截图：明确说「这不是行情图表」，读图仍给可见文字/版式摘要，`market_state="unclear"`、`levels=[]`，不触发关键位清单，检索最多留 1 条 |
| 11 | 条带编号走提示词（方案 A），不动图片像素 |
| 12 | 术语进 `GLOSSARY.md`：读图（`chart_read`）、图上事实、条带；**不新增 ADR**（不可引用已由 ADR-0002 覆盖，改回单阶段的代价是一个标志位） |

## 契约形状（spec 可直接抄）

```python
class Swing(BaseModel):
    kind: Literal["high", "low"]
    price: float | None        # 任一位不确定 → None，不推算
    strip: int                 # 1..N
    basis: str                 # 图上依据

class Level(BaseModel):
    price: float | None
    source: Literal["annotation", "swing", "axis", "unknown"]
    strip: int
    basis: str

class Focus(BaseModel):
    price: float
    why: str                   # 至少一句，按决议 2 的优先级

class ChartRead(BaseModel):
    instrument: str | None     # 图上未印 = None
    timeframe: str | None
    market_state: Literal["trend_up", "trend_down", "trading_range", "unclear"]
    swings: list[Swing]
    levels: list[Level]        # 全部可读位，不裁剪
    focus: list[Focus]         # 5–8 个，决议 1 第②层
    patterns: list[str]
    annotations: list[str]     # 图上文字原样
    indicators: list[str]      # 参数未知写「参数未知」
    unreadable: list[str]
```

## spec 还要自己定的（本轮未涉及）

- `chart_read` 阶段的失败分类（超时 / 非法 JSON / 空图）分别怎么记进 `failures`。
- 检索如何从 `market_state` + `focus` + `patterns` 生成 sub_queries（现 `retrieval_queries` 是文本路径）。
- 页面「读图数据」面板的具体字段与折叠位置。
- golden 的 10 张图从哪来、每张的 `expected_levels` 谁标注。
