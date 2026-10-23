# PAknow

多模态检索、证据图谱与可验证 SOP 记忆系统。术语以本文件为准：问题标题、证据引用、测试名、图表都用这里的说法。

## 语料与定位

**源文档 (Source Document, `doc_id`)**：
一份被摄入的 PDF 或 EPUB 版本，按内容寻址，可由 locator 唯一定位。
_Avoid_: book, 文件, 语料条目

**技术文档站 (`book/`)**：
本仓库用 MkDocs 讲系统实现与选型的站点，不是语料、不是源文档。
_Avoid_: doc, docs, 文档（不带限定词时）

**物理页 (`pdf_page_1based`)**：
PDF 文件内的第 N 页，从 1 起。与书上印的页码无关。
_Avoid_: 页码, PDF 页, page

**印刷页 (`printed_page_label`)**：
书页上印出的页码，可能重号、缺号或没有；只用于人工核对，不能作为唯一位置。
_Avoid_: 真实页码, 书页

**locator (`SourceLocator`)**：
指向源文档中一个唯一位置的组合：物理页 / 印刷页 / 图号 / 章节路径 / 页内区域，或 EPUB 的 spine 位置。
_Avoid_: 坐标, 位置信息, pointer

**图号 (`figure_label`)**：
作者在书中给图编的号（如“图 4.5”），用于引用和读者核对。
_Avoid_: 图片名, 文件名, 图 ID

**图资产 (`FigureAsset`, `figure_id`)**：
从页面裁出并内容寻址保存的图像文件。
_Avoid_: 图, 图片, image

## 证据

**证据单元 (`EvidenceUnit`, `evidence_id`)**：
可独立引用、可独立定位的最小文本单位（段落、标题、图注、表格等）；引用的锚点。
_Avoid_: 块, chunk, 片段, snippet

**父单元 / 子单元**：
超长证据单元按句子边界切开后的层级；子单元与父单元共享 locator，并指回父单元以恢复上下文。
_Avoid_: 分块, split, 子片段

**原文证据**：
从源文档文本层直接抽取的证据单元，是唯一可被引用的证据来源。
_Avoid_: 原始数据, source, 正文

**派生观察 (derived observation)**：
由视觉模型读取页面或图后生成的描述文本，只作索引与检索材料；永远不进入可引用证据。
_Avoid_: 图注, caption, OCR 文本, 图片描述

**来源 (provenance)**：
证据单元的文本是怎么产生的：原文抽取、OCR、或视觉模型派生。它回答“从哪来”，不回答“可不可信”。
_Avoid_: 质量, quality, 置信度

**证据充分性 (sufficiency)**：
对“现有证据是否足以回答”的判定，结论含缺失项、冲突项与可选的下一步检索。
_Avoid_: 置信度, 打分, 相似度

**有界补检索**：
充分性判定不足后发起的新一轮召回，轮数有上限，不做无限自循环。
_Avoid_: 重试, 循环检索, retry

## 图谱

**概念 (concept)**：
词典中人工校验的规范术语实体，由一个规范 id 与中英别名描述。
_Avoid_: 关键词, 词条, entity, 术语（不加限定）

**概念类型 (concept kind)**：
概念的一个属性——通用术语、可识别形态、市场环境、入场结构；不是新的节点类型。
_Avoid_: 节点类型, 分类标签

**概念身份 (concept identity)**：
表面写法到规范概念 id 的唯一解析路径。`configs/terms.yaml` 是唯一来源；规范 id 稳定、不改名，别名可以增删。对齐、分类构建与图谱扩展都经 `paknow/graph/identity.py` 的 `canonical_for` / `resolved_canonical` 解析；`TermMatch.canonical` 只是某次词典匹配的落点，不是跨模块身份。解析不到的表面写法进候选队列待人工审核，不新建节点。
_Avoid_: 别名表, 词条匹配, 同义词解析

**主张 (Claim)**：
作者在特定背景下做出的带条件断言（主体 + 断言 + 倾向 + 条件 + 语气）；主体指向规范概念，是待核验的候选，不是系统结论，也不含胜率。
_Avoid_: 事实, 规则, 信号, 建议

**条件 (qualifier)**：
主张成立所依赖的市场背景或语气限定（可能、通常、绝不）。
_Avoid_: 假设, 前提, 参数

**关系 (Relation)**：
图谱中带方向、带出处的语义边（如 SUPPORTED_BY、REQUIRES_CONTEXT）。
_Avoid_: 链接, 引用, link

**概念分类 (concept taxonomy)**：
概念节点之间由层级与例外关系（PART_OF、EXCEPTION_TO、INVALIDATED_BY、TRANSITIONS_TO）构成的子图，回答“这个形态属于哪一类、有哪些变体、在什么情况下失效”。它属于知识图谱，不是文档结构；来源可以是文档标题语法与目录，但不等同于书的目录。
_Avoid_: 分类树, 目录树, taxonomy

## 记忆

**运行轨迹 (`RunTrace`)**：
一次请求的完整情景记录：意图、路由、证据、输出与组件版本快照。
_Avoid_: 日志, trace, 链路追踪

**SOP（程序性记忆）**：
带适用条件、步骤、失败模式与停止规则的流程记忆，按版本与状态管理。
_Avoid_: 流程, playbook, 剧本, 模板

**SOP 状态**：
SOP 的晋升链：candidate → replay_validated → shadow → active → superseded / retired；只有经过离线回放才能离开 candidate，且只有 active 是权威方法。
_Avoid_: 阶段, phase, 等级

**用户事实 (`UserFact`)**：
用户明确陈述、带来源与有效期的偏好或约束。
_Avoid_: 画像, profile, 偏好设置

## 请求处理

**意图 (intent)**：
对用户问题类型的判定：知识问答 / 综述机制 / 图表分析 / 回测 / 学习路径。
_Avoid_: 模式, mode, 分类

**路由 (route)**：
为回答选定的、有独立打分的召回通道：混合文本 / 目录 / 视觉页 / 数值。
_Avoid_: 检索器, channel, 策略

**图谱扩展 (graph expansion)**：
召回之后沿关系边补充候选证据的步骤，不参与打分与融合；种子是查询词命中的概念与已召回证据里的概念。
_Avoid_: 图谱局部, 图谱这一路, graph route

**证据需求 (evidence need)**：
回答该问题所需证据的类别（定义、背景、图例、反例、对比、目录、周期）。
_Avoid_: 检索目标, query type

## 评测

**并集探针 (union probe)**：
评测用的上限参照：每条冻结扩展查询各自取 top-K 后求并集，不重排、不做多样性。它回答「召回集合里有没有这条证据」；不代表生产链路会把它交给生成。
_Avoid_: 基线（不加限定）, baseline

**生产等价基线 (production-parity baseline)**：
与线上文本通道同构的评测口径：同样的扩展查询、重排与多样性、同样的证据条数，只关闭图谱扩展。判定图谱扩展净增益只能用这个口径。
_Avoid_: 线上基线, 真基线

**召回宽度 (recall width)**：
文本通道经重排与多样性后真正进入提示词的证据条数。它与单查询 top-K 不是一回事，也不含图谱块。
_Avoid_: top-k, 召回量, 条数

**净增益 (net gain)**：
同一生产等价基线下，图谱模式的期望证据命中数减去文本模式的命中数。图谱补回文本自己丢掉的证据，不算增益。
_Avoid_: 增益, 提升, recall gain
