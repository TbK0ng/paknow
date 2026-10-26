# 08: 充分性判定看到真实证据

**What to build:** 充分性判定拿到的是带证据 id 与定位的真实证据，最多 20 条、每条 400 字，而不是被多样性压到 14 条、每条 120 字、只有序号的摘要。这样它才能指出"缺什么"，有界补检索也才真的会启动。判定规模写进运行轨迹，可审计。

**Blocked by:** None (can start immediately)

**Status:** resolved

- [x] 判定输入改为最多 20 条、每条 400 字，含证据 id 与定位；候选池更大时确实取满 20，不受多样性后 14 条的限制。
- [x] 运行轨迹记录实际送审的条数与字符数，且与真实送入的一致（不是"配置值"）。
- [x] 轮数上限仍为 3；判定失败仍 fail-open（不阻断回答）。
- [x] 判定输出契约不变：缺失项 / 冲突项 / 下一步检索。
- [x] 测试用 40 条命中的场景验证恰好 20 条进提示词、每条截断到 400 字，且不连库、不调模型。

## Comments

Resolved on `feat/pr-08-judge-evidence` (`2dd3baa`).

- `check_sufficiency` now sends at most 20 units of 400 chars each, one line per unit: `[n] 证据 <evidence_id> | <locator> | <text>` (whitespace normalised so the item count equals the line count). The locator is the shared `generate.graph_locator`, so the judge sees chapter path / PDF page / printed page / figure number. Missing/conflicting/next_query contract and the round cap (3) are untouched; failure still fails open.
- The pool is new: `RetrievalResult.candidate_hits` keeps the ranked hit list **before** `diversify` (the answer's 14-wide window no longer bounds the judge), and `_check_node` passes it, falling back to `hits` for callers without it. Generation widths are unchanged.
- The audit is actual, not configured: `check_sufficiency` returns `(Sufficiency, scale)` where `scale` is `None` when no prompt went out (zero hits or round cap), so `_record_node` keeps the scale of the last view the judge really read. It lands in `RunTraceRow.output.sufficiency` as `evidence_items` / `evidence_chars` alongside the verdict; the `run_query` response is unchanged.
- Smoke (scripted `run_query`, stubbed recall and model, real pipeline/node/judge formatting): 14 diversified hits, judge prompt 20 lines, every text exactly 400 chars, first line `[1] 证据 e0 | 第一章 › 第0节 · PDF第1页`, trace `evidence_items=20 evidence_chars=8721`, `rounds=3` (cap held).
- Tests: new `tests/test_sufficiency_judge.py` (40 hits → 20 items/400-char cap, id+locator, one line per unit, round cap sends nothing, fail-open keeps the scale, candidate-pool wiring, trace parity, capped round keeps the last judged view) plus the candidate-pool case in `tests/test_retrieval_pipeline.py`. Full suite 334 passed; `scripts/check_scratch.py` OK (light install, no DB/Qdrant/model).
- Deliberately not included: graph-expansion entries in the judge view. They are non-citable understanding aids and the verdict drives text re-retrieval, so the pool stays the text candidates.
- Docs updated: `book/chapter4-retrieval.md` §3 (the judge's pre-diversity view) and `book/chapter6-generation.md` §2 (judge input bound and traced scale).
