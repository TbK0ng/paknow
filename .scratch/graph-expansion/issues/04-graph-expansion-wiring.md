# 04: 图谱扩展接线

**What to build:** 文本召回不到的组合式问题，答案里能看到由概念分类与主张边补来的证据；这些证据与文本证据分区呈现、不可引用、超预算时按优先级截断，并且不改变文本通道的排序。

**Blocked by:** 01, 02, 03

**Status:** resolved

- [x] 种子两支并发：查询词经词典命中的概念先发、与文本召回并发；文本返回后用命中证据里的概念补一次。
- [x] 展开深度：概念分类 1 跳、机制链 2 跳；上限为概念 ≤8、边 ≤32、回取证据 ≤8、图块 ≤1,200 tokens；超限时机制链优先于分类树、证据相关性优先于边数量。
- [x] 回取证据带来源标记；已在文本结果中的不重复正文，只标注「已见 [n]」，引用编号与证据的绑定不被破坏。
- [x] 图谱上下文从整块序列化改为逐条紧凑行，每条带证据标识，并显式标注不可引用、仅作理解辅助。
- [x] 只在首轮与出现新概念的缺口轮执行；同一概念不重复展开。
- [x] 任一环节失败都不阻断回答，失败原因写入运行轨迹。
- [x] `graph_local` 从路由词汇中退出：路由只保留有独立打分的召回通道，图谱扩展单独命名。
- [x] 用一次实际查询验证：一个文本召回失败的组合式问法，答案上下文中出现图谱证据，且答案的引用只指向原文证据。

## Comments

Resolved on `feat/graph-expansion` (`7dc9910`, merged `0222fe1`; review fixes `e07e808`, merged `0fb9f53`).

- New `paknow/graph/expansion.py` plans and hydrates expansion; `retrieve_evidence` seeds from query dictionary hits concurrently with text recall, then adds concepts from returned evidence. Taxonomy is 1 hop; mechanism chain is concept -> aligned claim -> `SUPPORTED_BY` evidence, 2 hops.
- Budgets enforced: concepts ≤8, traversed candidate edges ≤32 (mechanism before taxonomy), evidence ≤8, graph block ≤1,200 tokens. Review fix made the edge budget count traversed candidates so it can actually bind (`truncated_edges`).
- Graph entries carry `provenance=graph` and `non_citable`; taxonomy entries hydrate from `edge.source`, mechanism entries from `claim.supporting_evidence`, with chapter/PDF/printed/EPUB locators. Text-seen entries render as `已见 [n]` without duplicating the body.
- Prompt renders one compact line per entry with `evidence_id` and an explicit `不可引用·仅理解辅助` marker. The `graph_context` alias was removed; `graph_evidence` is the single field used by pipeline/generation/eval.
- Runs on the first round and on gap rounds only; `retrieve_evidence(prior_graph_evidence=...)` appends and dedupes by `evidence_id`, so a round without new concepts preserves prior evidence. `graph_local` is removed from the route vocabulary; the router falls back to `hybrid_text` if filtering empties routes. Every failure is recorded in `failures` and generation continues.
- Smoke (`mech-07`: `突破在5分钟图上常见吗，什么样的突破才算强？`): 14 text hits / 18 citable; 8 graph entries (7 graph-only), including expected `964597e4…` missed by text; graph block present; `evidence_ids` = 17 citable IDs with zero graph-only IDs.
- Tests: `tests/test_graph_expansion.py`, `tests/test_retrieval_pipeline.py`, `tests/test_graph_origins.py`; full suite 71 passed. Review findings and fixes: `.scratch/graph-expansion/code-review.md`.
