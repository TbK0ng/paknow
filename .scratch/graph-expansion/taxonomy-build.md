# 02 概念分类边：真实库构建记录

日期：2026-10-23　分支：`ticket/02-taxonomy`　库：`sqlite:///E:/PAknow/data/paknow.db`

复现命令（在工作区根目录执行，`-m` 保证使用工作区里的 `paknow`）：

```bash
PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db E:/PAknow/.venv/Scripts/python.exe -m scripts.build_taxonomy          # dry run
PAKNOW_DB_URL=sqlite:///E:/PAknow/data/paknow.db E:/PAknow/.venv/Scripts/python.exe -m scripts.build_taxonomy --apply   # 写入
```

## 构建产出（两次 `--apply` 完全一致）

```
scope:      encyclopedia (doc_id=c46ebbbadd36d0d6584ad3330e89bf6d)
headings:   16644 loaded, 12 TOC titles excluded, 16632 parsed input
            -> 6245 (dropped 10387: header 8880, duplicate 1507)
parsed:     994  dropped_unparsed: 5251  coverage: 15.92%
relations:  EXCEPTION_TO=2  INVALIDATED_BY=181  PART_OF=3  TRANSITIONS_TO=142
edges:      328
fingerprint:b0a9aff4036c7c18
```

- 去重前 16,644 条标题，去重后 6,245 条；丢弃 10,387 条（页眉式重复 8,880 条，同文档重复 1,507 条）。
  页面噪声主体是 8,047 次出现的 `@喻无为`。
- **目录不参与主抽取**：开头约 10 页的目录表格行是 `paragraph` 块，`load_heading_units`
  只读 `block_type='heading'`，所以目录行从不进入解析；目录标题本身（`分类目录\nBPA
  ENCYCLOPEDIA` ×10、`未分类目录\nBPA ENCYCLOPEDIA` ×2）由 `is_toc_title` 显式剔除并
  计为 `excluded_toc=12`，只用于覆盖率核对。解析器/边集与剔除前逐字节相同
  （fingerprint 仍为 `b0a9aff4036c7c18`，328 条边），正文标题未被误删。
- 解析成功 994 条，解析不出关系而丢弃 5,251 条（覆盖率 15.92%），没有兜底 `PART_OF`。
- 994 条解析结果合并为 328 条唯一边（同一 `(from, relation, to)` 只保留字典序最小的 locator）。
- 328 条边全部来自 `origin='heading_parse'`；`origin='seed'` 的 8 条人工边、`claim_extract` 1,888 条、`figure_extract` 1,529 条在两次重建后数量不变。

两次运行对比：边行集合逐行相同（328 vs 328，`sorted(lines) ==` 为真），fingerprint 均为
`b0a9aff4036c7c18`；第二次 `origins before` 已是 `heading_parse=328`，重建后仍为 328，
说明重复运行幂等（`edge_id` 由 `(from_node, to_node, relation, origin)` 稳定生成）。

locator 完整性抽查：328/328 条 heading 边都能回指到存在的 `evidence_units` 行，且都有
`pdf_page_1based` 与 `chapter_path`；328 条边的 `edge_id` 两两不同。

## 实际查询样例

`taxonomy_neighbors(["wedge"])`（楔形）返回 58 条边，四类关系齐全：

```
PART_OF 1 | EXCEPTION_TO 1 | INVALIDATED_BY 31 | TRANSITIONS_TO 25
```

代表性行（原样 JSON；`direction` 相对被查询概念）：

```json
{"from": {"concept_id": "failed_breakout", "label": "失败突破"}, "relation": "PART_OF", "to": {"concept_id": "wedge", "label": "楔形"}, "direction": "in", "origin": "heading_parse", "evidence_id": "94bbeb00f09f549082761927fcf7b968", "locator": {"chapter_path": ["双顶上方突破失败，所以是楔形顶"], "pdf_page": 1301, "printed_page": null}}
{"from": {"concept_id": "channel", "label": "通道"}, "relation": "EXCEPTION_TO", "to": {"concept_id": "wedge", "label": "楔形"}, "direction": "in", "origin": "heading_parse", "evidence_id": "d84cb1ec7deaad894051f5fd79d11599", "locator": {"chapter_path": ["多头通道"], "pdf_page": 808, "printed_page": null}}
{"from": {"concept_id": "auto_buying_pressure", "label": "买压"}, "relation": "INVALIDATED_BY", "to": {"concept_id": "wedge", "label": "楔形"}, "direction": "in", "origin": "heading_parse", "evidence_id": "58ded7dd2cc4d4117a1df2bd0008ae68", "locator": {"chapter_path": ["开盘跳空低开：失败的抛物线楔形底"], "pdf_page": 2703, "printed_page": null}}
{"from": {"concept_id": "wedge", "label": "楔形"}, "relation": "TRANSITIONS_TO", "to": {"concept_id": "auto_nested_wedge", "label": "嵌套楔形"}, "direction": "out", "origin": "heading_parse", "evidence_id": "16864a47dcf1e40496780d2f832331ff", "locator": {"chapter_path": ["楔形顶"], "pdf_page": 8453, "printed_page": null}}
```

即：上级概念 `failed_breakout` → `wedge`；例外 `channel` → `wedge`；失效情形 31 条；
相邻阶段 25 条。每条都可凭 `evidence_id` / `pdf_page` 回原文核对。

## 真实数据暴露并修复的缺陷

1. **重复概念节点**：词典里 `auto_trading_range_tr`、`auto_failed_breakout_fbo` 等
   章节派生条目与基础词条共享别名（`交易区间`、`失败突破`）。`find_terms` 返回 `auto_*`
   canonical，会新建与 `trading_range` 同名的概念节点。现在解析时用
   `canonical_for(alias)` 归一到首次注册的 canonical；重建后新增节点从 3 个降到 1 个
   （仅 `swing`，无同名冲突）。边数由 330 变为 328，`auto_*` 边由 143 条降到 100 条
   （剩余 `auto_*` 是确有独立语义的条目，如过度优化陷阱、嵌套楔形）。

   注：写入后库里共有 154 个概念节点，其中 114 个没有任何边；包含一个无边的
   `auto_trading_range_tr`（label 仍是 `交易区间`）。该节点不是标题解析产物——328 条
   heading 边不引用它，且重建只为边上的概念建节点、不删除其他来源的节点。它是并行流程
   在同一真实库中写入的词典节点，`taxonomy_neighbors` 不会返回它。
2. **解析范围未限定**：重建原先扫描全库 19,194 条 heading（含 2,550 条书内标题），
   统计口径与百科无关。现在 `rebuild_taxonomy(doc_ids=...)` / `load_heading_units`
   支持按文档限定，CLI 默认取源文件名含 `encyclopedia` 的文档（可用 `--doc-id` 覆盖）。
3. **重建只删部分 heading 边**：原先删除带 `relation IN (taxonomy)` 过滤，若解析器关系
   集合变化会留下陈旧边。现在按 `origin='heading_parse'` 全量替换。
4. **邻居查询排序不确定**：同一 `(relation, from, to)` 上可能并列 heading/seed 边，
   原排序无 tie-breaker。现在追加 `origin, source, edge_id`。

对应测试：`tests/test_taxonomy.py` 新增 4 条（别名归一、文档范围、按 origin 全量替换、
并列边排序稳定）。
