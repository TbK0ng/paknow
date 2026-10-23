# 03: 主张→概念对齐

**What to build:** 从任一概念出发，系统能沿主张边拿到跨书、带条件的断言，并回到支撑它的原文证据；不再有 1,610 条主张因为主体没挂上概念而无法被机制问题用到。

**Blocked by:** None (can start immediately)

**Status:** resolved

- [x] 先用确定性手段对齐：词典别名与主体规范化，命中即写入规范概念，不调用模型。
- [x] 确定性未命中的残差再走批量模型对齐，带置信度阈值；低于阈值的不写入。
- [x] 对齐结果输出三组数字：原有已挂概念数、本票新增对齐数、残差数；残差清单可供人工后续处理。
- [x] 本票不做人工复核，对齐状态沿用待核验，不进入可引用证据集。
- [x] 每条主张边带来源标记（主张抽取），并与标题解析边、人工种子边可区分。
- [x] 重复运行幂等：不产生重复边，不改变已对齐结果。
- [x] 用一次实际查询验证：给定一个机制概念，能拿到若干条带条件的主张及其支撑证据。

## Comments

Resolved on `feat/graph-expansion` (`08709bf`, merged `f74a418`).

- Deterministic: exact alias -> single dictionary match; multi-match/no-match becomes residual. Model fallback is batched with a 0.8 threshold and fails open.
- Real DB: 278 already aligned -> +504 deterministic -> +109 model = 891/1,888; residual 997 across 721 subjects.
- Second `--apply` wrote 0 new alignments; `extraction_status` unchanged (pending, not citable); all `SUPPORTED_BY` edges remain `origin='claim_extract'`.
- Residuals: `.scratch/graph-expansion/claim-residuals.json`; summary `.scratch/graph-expansion/claim-alignment.md`.
- Query verified: `concept_lookup(["双底多头旗形"])` returns conditioned claims with supporting evidence.
- `paknow/gateway.py` now creates the SDK client lazily so scripts import without an API key.
- Tests: `tests/test_align.py`.
