# PA Agent

> 一个可审计的价格行为研究 Agent：确定性规则负责提名候选，PAknow 负责从六本原书里检索并核验证据，LLM 只在证据完整后做结构化复核，程序再按已收盘 K 线逐根回放。

这个项目面向 Agent 系统开发，重点不是“让模型自由决策”，而是解决三个工程问题：

1. 如何把 LLM 放进一个可验证、可回放的决策边界。
2. 如何让 RAG 的引用可追溯、可逐字核验。
3. 如何保证回放只使用信号当时可见的数据，不使用未来信息。

项目只做离线模拟，不连接交易账户，不下实盘订单。

## STAR 速览

| 维度 | 内容 |
| --- | --- |
| Situation | 价格行为研究依赖人工读图，容易受主观判断、LLM 幻觉和后视偏差影响；普通 RAG 只能返回相似段落，不能证明引用真的支持当前候选。 |
| Task | 构建一个本地研究系统：先确定性提名候选，再检索可引用原文，证据完整后才允许模型复核，最后用后续已收盘 K 线做无未来数据的模拟回放。 |
| Action | 实现行情校验、PA 规则引擎、父子段落建库、BM25 与 dense 混合召回、rerank、审校图谱规划、证据包组装、结构化输出校验、版本化复核账本、批量队列和单页研究工作台。 |
| Result | 当前封存语料包含 6 份文档、28,615 条 evidence、16,067 个 dense points 和 24 条 reviewed graph lead；本机冻结快照基准中，8 路本地取证约 9.56 笔/秒，达到同次测试峰值的 96%；测试覆盖证据门禁、伪引用、未来价格锚点、语料变更、批量快照和 provider 失败等关键性质。 |

## 1. Situation

价格行为研究有三个天然问题：

- **主观性**：同一张图，不同交易者可能得出不同结论。
- **幻觉**：LLM 可以生成听起来合理、但没有原文依据的解释。
- **后视偏差**：回放时如果不严格限制可见窗口，很容易把信号之后的走势混进判断。

普通 RAG 方案通常只能做到“召回相似段落”。它不能回答以下问题：

- 这段原文真的来自当前允许的语料版本吗？
- 它是支持、反证，还是确认条件？
- 模型引用的句子是否真的在原文中逐字存在？
- 模型给出的价格是否真的来自当前 81 根 K 线？
- 这次结论在行情、规则、语料或模型配置变化后是否还成立？

PA Agent 的设计就是围绕这些问题展开。

## 2. Task

目标不是做一个“会聊天的交易机器人”，而是做一个可审计的研究工具：

1. 用确定性规则筛选候选，避免让 LLM 直接决定方向。
2. 从六本价格行为原书中检索原文，而不是让模型凭常识回答。
3. 只有 support、risk、confirmation 三类证据都完整时，才允许调用模型。
4. 模型输出必须是结构化对象，并经过引文、角色、行情锚点和价格几何校验。
5. 只有合法 verdict 才进入复核账本和模拟回放。
6. 行情、规则、语料、模型或策略版本变化时，旧结论必须失效或可解释地复用。

## 3. Action

### 3.1 系统架构

```text
OKX 已收盘 15 分钟 K 线
        |
        v
行情校验与因果窗口
        |
        v
PA 规则引擎提名候选
        |
        v
80 根背景 + 1 根信号
        |
        v
审校图谱规划 + BM25/dense 混合召回
        |
        v
证据包组装与逐字核验
        |
        v
LLM 结构化复核
        |
        v
引文、角色、行情锚点、价格结构校验
        |
        v
SQLite 复核账本
        |
        v
离线逐根 paper replay
        |
        v
单页研究工作台
```

### 3.2 行情与因果窗口

- 只读取 OKX 公共历史 K 线，默认保留 480 根可见 K 线和 232 根指标预热数据。
- 只接受已收盘、连续、OHLCV 有效的 K 线，不用合成数据补缺口。
- 下载失败时只允许回退到之前验证过的真实快照，并明确标记 `stale`。
- 每个候选固定使用信号前 80 根已收盘 K 线加当前信号 K 线，共 81 根。

实现见 [pa_market.py](paknow/pa_market.py) 和 [pa_context.py](paknow/graph/pa_context.py)。

### 3.3 第一层：确定性 PA 规则

[pa_rules.py](paknow/pa_rules.py) 使用 EMA20、ATR14、15 分钟趋势和 1 小时趋势检查五个条件：

- `trigger`：同向收盘确认
- `local_trend`：15 分钟趋势支持方向
- `higher_trend`：1 小时没有强反向趋势
- `pullback`：近期回踩靠近 EMA20
- `second_test`：二次测试或 EMA 代理条件

只有全部通过，才生成参考入场、失效位和目标位。[pa_swing.py](paknow/pa_swing.py) 再检查入场、止损、目标是否构成有效价格区间，并计算扣除成本后的 `net_rr`。不构成有效结构的候选会被标记为 `blocked`。

规则层只负责“提名”，不负责解释，也不调用模型。

### 3.4 知识库与证据检索

六份 PDF/EPUB 原件的文件名和 SHA-256 固定在 [pa_corpus_sources.yaml](configs/pa_corpus_sources.yaml)。导入流程会：

1. 校验原件哈希。
2. 解析 PDF/EPUB，保留页码、章节路径、图注和出处。
3. 按 `passages-v2` 规则切块。
4. 对长段落保留父段和子段，子段带相对父段的字符偏移。
5. 把原文写入 SQLite，作为权威证据库。
6. 建立 Qdrant 的 BGE-M3 dense 向量和 `bm25_v1` sparse 向量。

SQLite 是原文权威来源，Qdrant 只是派生索引。检索结果最终会通过 [citations.py](paknow/evidence/citations.py) 从 SQLite 重新 hydrate，避免相信向量库 payload。

检索链路包括：

- BGE-M3 dense 召回
- BM25 sparse 召回
- Qdrant RRF 融合
- BGE reranker 重排
- 相邻段落扩展
- 按文档去重和限流
- 进程级 single-flight LRU 缓存
- 手动请求优先于批处理

实现见 [search.py](paknow/retrieval/search.py)、[pipeline.py](paknow/retrieval/pipeline.py) 和 [runtime.py](paknow/retrieval/runtime.py)。

### 3.5 图谱规划与证据包

这里的图谱不是全量知识图谱，而是一个经过人工审校的小型“概念 -> 条件问题 -> 原文”图。

- 规范概念 ID 只来自 [terms.yaml](configs/terms.yaml)。
- 只接受 `source_scope=pa_text_graph` 且 `extraction_status=reviewed` 的 claim 和 edge。
- claim 必须恰好有一个 supporting evidence。
- 必须同时存在概念到 claim 的角色边，以及 claim 到 evidence 的 `SUPPORTED_BY` 边。
- 证据角色固定为 `support`、`risk`、`confirmation`。

[pa_packet.py](paknow/retrieval/pa_packet.py) 负责最终证据包：

1. 根据候选 setup 规划三类问题。
2. 每个角色执行图谱问题和确定性 fallback 问题。
3. 合并文本召回和图谱 lead。
4. 从 SQLite 重新读取原文。
5. 只接受 `native_text` 或 `ocr`。
6. 排除页脚、标题、父段和长度不合适的文本。
7. 只有原文哈希与图谱 claim 中的 SHA-256 完全一致，才标记 `verified=true`。
8. 选择最多 5 段、角色独立、父子段落不重复的证据。

如果图谱不可用，系统可以继续文本检索，但不会把普通文本伪装成已核验图谱角色。缺少关键角色、父段缺失或哈希不一致时，会产生 evidence gaps，并阻止后续模型复核。

### 3.6 受约束的模型复核

[pa_analysis.py](paknow/pa_analysis.py) 的 `explain()` 是统一分析入口：

- 普通 K 线：生成问题、文本检索、可选模型解读。
- PA 候选：先构造 setup 和证据包，证据完整后才进入模型槽。
- 本地 commentary 始终生成，即使云端不可用。

模型输入包含：

```json
{
  "rule": "确定性规则结果",
  "previous_80_oldest_first": "信号前 80 根 K 线",
  "signal_bar": "当前信号 K 线",
  "sources": "可引用证据包",
  "causal_facts": "可引用行情事实",
  "setup": "当前候选结构"
}
```

服务端不会因为 JSON 解析成功就相信模型。模型输出还要经过：

- Pydantic schema 校验。
- `evidence_ids` 必须来自本次输入。
- `market_refs` 必须是本次 `causal_facts` 的键。
- `quote.text` 必须是来源原文中的逐字连续子串。
- `thesis`、`risks`、`watch` 必须分别引用 `support`、`risk`、`confirmation` 角色。
- 三类证据不能共用同一个 passage。
- `paper_plan` 必须保持原候选方向。
- 入场、止损、目标必须构成正确价格顺序。
- 每个非空计划价格必须映射到 81 根 K 线中的真实时间和 OHLC 字段。
- `next_open` 不能虚构未来开盘价。
- 需要收盘确认时，必须使用 `close_confirm` 或 `pullback_confirm`。

[PaperPlan](paknow/pa_plan.py) 只允许固定结构化字段，不执行模型生成的代码或任意指令。

### 3.7 版本、幂等与回放

候选指纹包含策略版本、规则版本、语料 generation、云模型配置、embedding/rerank 模型、完整规则状态和完整 81 根 K 线。任何一项变化都会让旧结论失效。

[ReviewLedger](paknow/pa_review.py) 分开保存：

- `reviews`：证据完整、模型成功、判定合法的最终结果。
- `attempts`：已经实际调用模型但失败的尝试。

同一候选已有有效结论时不会重复调用模型；已有失败尝试时，默认读取上次失败记录，只有显式传入正确的 `retry_attempt_id` 才允许重试。

[pa_replay.py](paknow/pa_replay.py) 只执行合法 `PaperPlan`：

- 从信号之后开始，不把信号 K 线当成已知成交。
- 支持下一根开盘、突破触价、回撤触价、收盘确认和回撤确认。
- 开盘跳过止损按开盘价成交。
- 同一根 K 线无法判断止盈止损先后时，保守地按止损处理。
- 收盘结构失效在下一根开盘离场。
- 每个候选独立模拟，不共享仓位。

### 3.8 API、批处理与前端

FastAPI 应用在启动时检查 Qdrant 和封存语料，任何一项不匹配都会拒绝启动。[main.py](apps/api/main.py) 负责生命周期检查，[pa_agent.py](apps/api/pa_agent.py) 提供主要路由：

- `GET /pa-agent/market`
- `GET /pa-agent/rule`
- `POST /pa-agent/analyze`
- `POST /pa-agent/review-evidence`
- `POST /pa-agent/review`
- `GET /pa-agent/review-history`
- `GET /pa-agent/review-history/{key}/replay`
- `GET/POST /pa-agent/review-batch`
- `GET /pa-agent/evidence/{id}`

`/review-evidence` 只做本地取证，不调用模型、不写账本；成功且证据完整时返回短期 `evidence_token`。`/review` 使用这个 token 复用证据包，避免重复检索。

[pa_batch.py](paknow/pa_batch.py) 是显式、快照固定的进程内批量队列。批量任务只处理没有有效结论的候选，同一时间只允许一个 batch；遇到认证失败、计费错误、上游限流、输入变化或连续 5 次模型失败时会停止派发，但在途请求会正常结束。

前端是单文件 [pa_agent.html](apps/viewer/pa_agent.html)，没有前端构建链。页面提供行情回放、候选定位、证据预览、模型复核、历史记录、批量状态和模拟单展示。

## 4. Result

### 4.1 可运行的完整闭环

当前工作区已经具备：

- 可启动的 FastAPI 服务。
- 与 Qdrant 封存清单匹配的 SQLite 证据库。
- 六份原书的纯文本检索索引。
- 本地复核账本和历史回放。
- 单笔复核、批量复核和停止派发。
- 通过校验后生成的条件式 paper plan 和逐根模拟。

当前封存清单见 [pa-corpus.json](data/objects/pa-corpus.json)：

| 指标 | 数量 |
| --- | ---: |
| 文档 | 6 |
| evidence | 28,615 |
| dense points | 16,067 |
| sparse points | 15,839 |
| 父段 | 737 |
| 合并段 | 2,705 |
| context-only 源块 | 10,554 |
| reviewed graph lead | 24 |

### 4.2 性能与并发

在本机使用 93 笔冻结 OKX 候选做的空检索缓存对照中，模型已加载、不含云调用：

- 8 路本地取证约 9.56 笔/秒。
- 达到同次测试吞吐峰值 9.96 笔/秒的 96%。
- 相比 16/24 路，8 路的单笔 P95 更低，因此作为运行上限。

这是本机基准，不是通用性能承诺。复测方式见 [pa-agent.md](docs/pa-agent.md)。

### 4.3 测试覆盖的关键性质

测试重点不是“页面能打开”，而是系统在边界条件下仍然可信：

- 证据不足时不会调用模型。
- 错角色引用、伪造引文、未知行情事实和未来价格锚点会被拒绝。
- 语料或行情变化后，旧复核不会继续冒充当前结论。
- 批量复核固定快照、去重、可停止，已发出的请求不会被强行杀死。
- Terra 流没有完整 `completed` 状态时，不会产生 verdict。
- 历史查询和回放不访问实时行情，也不重新调用模型。
- 嵌入式 Qdrant 锁冲突、缺失集合、空集合和 BM25 未完成都会拒绝启动。

## 5. 快速开始

### 环境要求

- Python 3.14
- 可用的 `data/paknow.db`
- 可用的 `data/objects/`，其中包含 Qdrant 和封存清单
- 首次建库需要六份本地 PDF/EPUB 原件

`data/paknow.db` 和 `data/objects/` 必须成对匹配，不能只替换其中一个。

### 安装与启动

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

可从 [.env.example](.env.example) 复制一份 `.env` 配置模型。不要提交密钥。

```powershell
.\.venv\Scripts\python.exe -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8765
```

打开 <http://127.0.0.1:8765/>，会直接进入 `/pa-agent`。

### 首次建库

首次安装请按 [建库说明](docs/pa-agent.md#首次建库) 在隔离目录导入六份原件并封存。建库使用嵌入式 Qdrant，同一时刻只允许一个进程访问。

## 6. 项目结构

```text
apps/api/            FastAPI 路由、启动检查和请求契约
apps/viewer/         单页研究工作台
paknow/pa_market.py  行情下载、快照、因果窗口
paknow/pa_rules.py   确定性 PA 规则
paknow/pa_analysis.py
                     模型输入、结构化输出和强校验
paknow/pa_review.py  指纹、复核账本、并发控制
paknow/pa_replay.py  逐根 paper replay
paknow/pa_batch.py   快照固定的批量队列
paknow/retrieval/    混合检索、重排、缓存和证据包
paknow/ingest/       PDF/EPUB 解析、切块和持久化
paknow/graph/        概念身份和审校图谱规划
paknow/evidence/     引用 hydration 和出处渲染
scripts/             建库、封存、评估和基准工具
tests/               安全性质、契约和回放测试
```

## 7. 工程取舍

| 决策 | 原因 | 代价 |
| --- | --- | --- |
| 规则先提名，模型后复核 | 避免 LLM 直接决定方向 | 规则覆盖有限，候选数量受规则约束 |
| 证据不完整不调用模型 | 防止模型用常识补证据 | 部分候选会直接显示缺口 |
| SQLite 作为原文权威源 | 向量库 payload 可能变化或损坏 | 检索后需要一次 hydration |
| 父子段落和字符偏移 | 保留限定语和阅读上下文 | 建库和校验逻辑更复杂 |
| 小型 reviewed 图谱 | 高精度、可审计 | 当前只有 24 条 lead，覆盖范围小 |
| 内容寻址指纹 | 行情、语料、模型和策略变化可追踪 | 旧结论不能无条件复用 |
| 进程内批量队列 | 本地部署简单 | 服务重启后不恢复队列 |

## 8. 边界与下一步

当前项目明确不是：

- 自动交易系统。
- 实盘下单工具。
- 通用金融问答机器人。
- 可水平扩展的多用户 SaaS。

当前限制：

- 单进程 loopback 架构，默认没有认证和公网防护。
- 嵌入式 Qdrant 同一时刻只允许一个进程。
- 扫描 PDF 不执行 OCR，只记录 OCR 候选。
- reviewed graph 覆盖较小，仍需要更多独立标注样例。
- 前端是单文件实现，浏览器端端到端测试仍可加强。
- wheel 目前只声明打包 `paknow`，按源码目录运行是主要交付方式。

下一步优先方向：

1. 增加浏览器端 E2E 测试，覆盖初始化、回放、取证和批量状态。
2. 扩大 reviewed graph 的覆盖，重点补充未收录的反例和不同趋势背景。
3. 在同一份证据包上做模型成本、延迟和概率校准对照。
4. 如果扩展到多用户，把进程内队列、缓存和锁迁移到持久化任务系统和远程 Qdrant。
5. 如果暴露到局域网，增加认证、权限和反向代理。

## 9. 验证

```powershell
python scripts/check_scratch.py
python -m pytest -q
```

CI 使用 Python 3.14，执行同样的仓库检查和测试，见 [.github/workflows/check.yml](.github/workflows/check.yml)。

## 10. 文档

- [行情范围、回放规则、复核和排错](docs/pa-agent.md)
- [从原文到候选复核的取证流程](docs/pa-rag.md)
- [本地检索和离线维护](docs/agents/retrieval-dev.md)
- [上游规则版本与第三方许可](docs/licenses/pa-agent-notice.md)
