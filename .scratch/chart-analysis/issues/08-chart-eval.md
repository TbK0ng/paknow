# 08: 图表评测：带图接缝、三个率、10 张 golden

**What to build:** 一条能跑的图表评测：题集条目带图片引用（哈希 + 相对路径），评测器按题加载图片跑完整链路，判定只做确定性部分，输出漏读率、多读率、漂移率三个数字。题集是独立数据集，自带结构校验；缺图记 skipped 而不是失败。

**Blocked by:** 01

**Status:** resolved

- [ ] 评测器能按题集加载图片并传入链路（复用 `run_query` 已有的 `images` 参数，不新增执行路径）
- [ ] 判定为纯函数，输出漏读 / 多读 / 漂移三个率；命中要求整条价位一致，不设容差
- [ ] 四个组合各有单元覆盖：命中、漏读、多读、末位漂移
- [ ] 题集独立成 `chart_v1`，含 10 张图的 `expected_levels` / `expected_swings` / `expected_state`，只存 `image_sha256` 与相对路径，图片不进 git
- [ ] 题集自带结构校验测试，与 60 题文本集的校验互不干扰；60 题文本集与其产物不变
- [ ] 图片缺失或哈希不匹配时该题记 skipped，不判失败，并计入汇总
- [ ] 跑一次能在产物里读到三个率与逐题明细（漏读/多读/漂移分别列出价位）

## Comments

**Resolved** in `83d839a`/`2adb17c`, merged as `736f9d2`；`judge_levels` 的漂移配对缺陷复审后由 `3fc849c` 修正（先整体精确匹配，再在剩余项里配漂移）。

- `scripts/eval_chart.py`：题集 → `run_query(question, images=[data], expand_graph=False)`；图片缺失/哈希不符记 `skipped`（不进任何分母），`--resume` 只重试 missing/errored。
- 判定为纯函数：`miss=漏读/期望`、`extra=多读/已读`、`drift=漂移/(命中+漂移)`，命中要求整条一致，无容差，不调盲判模型。
- `evals/gold/chart_v1.json`：10 题，仅存相对路径 + `image_sha256`，图片在 `data/market/charts/`（gitignored）；结构校验独立成 `tests/test_gold_dataset_chart.py`，60 题文本集与产物未动。
- 实跑 2 题：`miss 49/56 (87.5%)`、`extra 1/8 (12.5%)`、`drift 0/7`；chart-02 那次读图返回 `unstructured`，全部期望位计为漏读——这是读图不稳定的真实读数，不是判定错误。
- 口径风险：`expected_levels`/`expected_state` 由一次 `chart_read@deepseek-flash` 得出，未经人工确认；`expected_swings` 均为空。首次 10 题全量运行应作基线，人工核对后替换。
- `.scratch/chart-analysis/eval/dev/` 是上述 2 题运行产物；`summary.json` 的 `dataset_sha256` 记录的是修 CRLF 之前的字节，与当前 sidecar 不一致。
