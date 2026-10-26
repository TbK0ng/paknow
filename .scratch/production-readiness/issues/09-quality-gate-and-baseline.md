# 09: 本地质量门禁与入库基线

**What to build:** 一条本地命令在提交前回答"这批改动有没有牺牲答案质量"：默认按四层（百科 / 机制 / 例外 / 条件主张）抽样 10 道题跑确定性档，硬约束失败无条件退非零，软指标按已测噪声地板容差；基线入库，可逐题复核。它不进 CI，CI 只跑门禁的确定性单测。

**Blocked by:** 01, 02, 03

**Status:** resolved

- [x] 门禁有两档：确定性档（不调云模型）与追加盲评档；默认只跑 10 题分层抽样。
- [x] 硬约束任一条失败即退非零：引用越界、图谱独有证据被引用、答案为空（降级占位按 03 的分类识别，不靠文本长度）。
- [x] 软指标有明确地板：判定分单维度差 < 0.12、无据主张总数差 < 15 视为未变化；冻结模式下覆盖率容差收紧到 0.03。
- [x] 基线与当次运行记录一起入库（ADR-0004），只由显式命令重录；基线带数据集哈希，数据集改动会拒绝误用旧基线。
- [x] 门禁脚本的确定性单测在现有 CI 里跑通：不 import torch、不连库、不调云模型，CI 不新增 job。
- [x] 用一次真实确定性运行验证退出码：未改动代码对基线为 0；把基线里任一硬指标改坏后为 1。

## Comments

- `scripts/quality_gate.py`，两档共用一个 10 题分层样本：`stratified_sample` 按各层大小用最大余数法分配（20/20/10/10 → 3/3/2/2），层内用 `eval_judge.spread` 均匀取样（两头都取），最后按数据集顺序输出；每层 2~3 题，样本是数据集的函数而非随机。
- `--deterministic`（默认）不调云模型：直接用生产链自己的 `_route_node` / `_read_node` / `_retrieve_node`，注入金题的 `frozen_plan`，所以路由、充分性判定、生成三个模型节点都不会走到，而查询装配（含 SOP 额外查询、6 槽上限）、召回、重排、多样性、图谱扩展仍是生产代码路径而不是副本。逐题记录 `queries` / `query_hash` / `citable_ids` / `graph_evidence_ids` / 覆盖率 / 检索失败。硬约束：检索抛错（`retrieval_error`）、可引用集合为空（`no_citable_evidence`，生成只能给出空答案）。
- `--judge` 先跑同一档确定性检查，再跑完整链路（`run_query(plan=...)`，即 ticket 01/02 的冻结口径）并逐题盲评（复用 `eval_judge.judge_record`）。三条硬约束在这里由 `eval_judge.hard_constraints` 从存下来的 id 重算：引用越界、图谱独有证据被引用、答案为空；`degraded` 降级占位按 03 的分类标记判为空答案（`hard_constraints` 增加一行 `record.get("degraded")`），不靠文本长度。
- 软地板（`compare_block`）：覆盖率较基线下跌 ≥ 0.03、判定任一维度均值下跌 ≥ 0.12、无据主张每臂总数增长 ≥ 15 即退化；变好不退非零（地板只拦退化，不拦改善）。地板直接取 spec 已测噪声：同一批答案重判臂均值 ±0.07~0.12、无据主张总数 ±15~31。
- 基线 `.scratch/production-readiness/quality-gate/baseline.json`，带 `dataset_sha256`（`7cf35d94…`，与 `evals/gold/graph_expansion_v2.sha256` 一致）与 `sample`；哈希或样本不符时在任何检索开始前打印 `refused …` 并退 1。只有 `--record` 写基线，跨档合并（`--record` 保留已有 judge 块，`--record --judge` 两块都录），有硬失败的运行拒绝入基线。运行记录 `records.jsonl`（确定性逐题）、`judge.records.jsonl`（盲评逐题）、`summary.json` 写到 `--out`，与基线一起入库。
- 入库基线（真实运行，主库 `E:/PAknow/data/{objects,paknow.db}`，工作树代码）：确定性 text 10/10 硬通过、覆盖率 0.450、0 检索错误；graph 10/10、0.450、0。盲评 20/20 已判、0 错误：text 硬 10/10、correctness 1.40 / condition_preservation 1.60 / citation_support 1.20、无据主张 19；graph 硬 10/10、1.30 / 1.60 / 1.20、无据主张 20。运行记录 `20261026T033710Z/`（确定性）与 `20261026T034000Z-judge/`（含确定性 + 盲评，wall 1045s）。
- 退出码实测（真实调用，主库）：未改动代码对基线 → `quality gate: pass`，**exit 0**；把基线里 text 臂 `hard_pass` 从 10 改成 11 → `FAIL deterministic text: hard_pass 10 < baseline 11`、`quality gate: 1 regression(s)`，**exit 1**；把基线 `dataset_sha256` 改成 64 个 0 → `refused …: dataset_sha256 … != 7cf35d94…`，在任何检索之前 **exit 1**。
- 测试：`tests/test_quality_gate.py`（16 个：分层抽样与顺序、硬失败退 1、有硬失败拒绝入基线、录音带哈希/样本/运行记录、覆盖率 0.03 地板两侧、判定分 0.12 地板两侧、无据主张 15 地板两侧、数据集哈希/样本拒绝且不启动检索、缺基线不自动创建、judge 档缺 judge 块拒绝）与 `tests/test_eval_judge.py` 新增的降级占位判空答案；全部纯函数/桩，不 import torch、不连库、不调模型。`pytest tests -q` → 392 passed；`check_scratch.py` → 31 files OK；CI 未新增 job。
- 文档：`docs/agents/retrieval-dev.md` 新增「Local quality gate」命令块（两档、地板、基线位置、退出码、重录、运行记录）。
