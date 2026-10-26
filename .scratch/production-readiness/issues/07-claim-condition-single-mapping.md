# 07: 主张条件唯一映射与渲染规则

**What to build:** 主张的条件只有一处读取映射：写入端存的背景与语气在此对齐为渲染所需的条件，渲染只消费这一处输出。有条件的主张把条件与断言写在同一行；没有条件的主张不出现任何条件字样——不兜底成"无条件"，也不编造条件。

**Blocked by:** None (can start immediately)

**Status:** resolved

- [x] 存在单一映射函数：写入端的背景/语气键在此转换，渲染端不再各自去翻不同键名。
- [x] 有条件的主张：渲染出的那一行同时含该条件文本与主张内容。
- [x] 无条件的主张：那一行不含条件字样（"无条件"、"通常在"之类的兜底措辞都算失败）。
- [x] 图谱扩展候选水合里对已停止使用的旧条件键的读取被删除（库中这些键恒为空）。
- [x] 两个方向各有一个纯函数测试；条件取自库里真实存在的键，不用假造键。

## Comments

- `generate._claim_conditions` is now the only mapping point: it takes the stored `qualifiers` mapping and renders `context` as 背景 and `modality` as 语气; `render_graph_context` consumes only its output.
- Deleted the dead `conditions`/`exceptions` reads from `expansion.load_mechanism_candidates`; hydrated claims carry the stored `qualifiers` only. `contracts.Claim` comment now names the two real keys.
- Two render-direction tests on the real stored shape plus a unit test for `_claim_conditions({"context": ..., "modality": "may"})` and `{}`; the old test faking legacy keys was replaced.
- Smoke: temp SQLite with `qualifiers={"context": ["强趋势"], "modality": "usually"}` → `load_mechanism_candidates` → `render_graph_context` yields `突破 → 常常失败（条件：背景=强趋势；语气=usually）`, and an empty-`qualifiers` claim renders without any 条件 wording.
- No writer change and no backfill: `extract.py` still stores `context`/`modality`.
