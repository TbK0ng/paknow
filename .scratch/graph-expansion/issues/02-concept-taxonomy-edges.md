# 02: 概念分类边

**What to build:** 给定一个概念，系统能列出它属于哪一类、有哪些变体、在什么情况下失效、下一个阶段是什么。这些边来自百科标题语法的确定性解析，不靠模型猜。

**Blocked by:** None (can start immediately)

**Status:** resolved

- [x] 解析前先对标题类证据单元去重：同文档内归一化文本相同者保留首次出现，出现次数达到阈值的页眉样式标题整体丢弃；去重前后数量作为可见产出。
- [x] 「：」用于切分情境与条目；`失败` 产出 `INVALIDATED_BY`，`随后` 产出 `TRANSITIONS_TO`，`但…恢复/继续` 与 `例外` 产出 `EXCEPTION_TO`，其余只在能识别出明确上级概念时产出 `PART_OF`。
- [x] 解析不出关系的标题直接丢弃，不兜底成 `PART_OF`；丢弃数量与解析覆盖率作为产出。
- [x] 不新增关系类型，只使用既有关系枚举。
- [x] 每条边带来源标记（标题解析 / 主张抽取 / 人工种子），带可回指的 locator；重建时不影响人工种子。
- [x] 同输入重复运行产出完全相同的边集合（顺序无关）。
- [x] 提供按概念查询变体、失效情形与相邻阶段的能力，并用一次实际查询验证结果。
- [x] 标题去重与语法解析以纯函数形式被直接测试，覆盖：映射正确性、丢弃行为、来源标记。

## Comments

Resolved on `feat/graph-expansion` (`5c3116d`, merged `4f6f617`).

- Pure parser: `paknow/graph/taxonomy.py` (dedupe, grammar, rebuild, neighbors); CLI `scripts/build_taxonomy.py`.
- Real DB: 16,644 headings -> 6,246 after dedupe (header 8,890 / duplicate 1,508); 994 parsed (15.91% coverage); 328 edges: PART_OF 3, EXCEPTION_TO 2, INVALIDATED_BY 181, TRANSITIONS_TO 142.
- Repeat run produced the identical edge set; only `origin='heading_parse'` is replaced; seed (8), claim (1,888) and figure (1,529) edges unchanged.
- Query verified: `taxonomy_neighbors(["wedge"])` returns 58 edges with `evidence_id` and chapter/page locators.
- Notes: `.scratch/graph-expansion/taxonomy-build.md`; tests in `tests/test_taxonomy.py`.
