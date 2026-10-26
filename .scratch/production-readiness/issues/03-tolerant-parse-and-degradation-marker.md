# 03: 结构化输出容错与降级可机读

**What to build:** 模型输出里出现裸换行、制表符，或尾部跟着第二个 JSON 对象时，用户仍然拿到答案（取第一个完整对象）。确实无法解析时，错误带可机读的失败分类与原文片段；答案被降级时，响应与运行轨迹都明确标记，用户看到"本次生成失败、请重试"而不是把占位文字当作答案。

**Blocked by:** None (can start immediately)

**Status:** resolved

- [x] 解析顺序：严格解析 → 容忍控制字符 → 用增量解码取出第一个完整 JSON 对象；三者都失败才报错。
- [x] 裸换行/制表符可解析；两个连续对象只取第一个，其后内容忽略；先前的严格校验对缺字段仍然报错。
- [x] 结构性截断仍报错，错误携带失败分类（解析 / 超时 / 无内容）与原文前 120 字。
- [x] 降级答案在 HTTP 响应与运行轨迹中带 `degraded: true` 与同一分类；答案正文保留"本次生成失败"的可见提示。
- [x] 占位答案可与真实的短答案区分（凭分类字段，不靠文本长度猜测）。
- [x] 读图路径已有的失败分类不被改成第二套词汇。

## Comments

- `gateway.chat_json` 现在的解析顺序是 `json.loads` → `json.loads(strict=False)` → `JSONDecoder(strict=False).raw_decode`（从第一个 `{` 起），三步都失败才抛 `ModelOutputError`；它携带 `reason`（`invalid_structure` / `no_content`）与 `snippet`（原文前 120 字），且是 `ValueError` 子类，与读图路径的分类兼容。schema 校验仍在解析之后，缺字段照旧抛 `ValidationError`。
- 分类映射 `failure_reason` 移到 `paknow/gateway.py`，由 `chart.read_chart` 与 `generate` 共用；`chart._failure_reason` 已删除 —— 只有一套 `timeout` / `invalid_structure` / `no_content`。
- 降级答案在 `generate` 里带 `degraded: true` 与 `degradation: {reason, detail}`；`_generate_node` 把分类写进 `failures`（`generate: <reason>: <detail>`），`_record_node` 的 `output` 保留同一标记；`_resolve_answer` 白名单透出 `degraded` / `degradation`，正文仍是「本次生成失败…请重试」。
- 测试：`tests/test_gateway_json.py`（控制字符、两个连续对象取第一个、截断报错带分类+片段、空内容 `no_content`、缺字段仍 `ValidationError`、分类词汇）与 `tests/test_chart_answer.py` / `tests/test_api_chat.py`（降级标记进 HTTP、进 trace，短真实答案不被标记）。
- 冒烟（无 DB、无云调用，真解析路径 + 假 `_create`）：控制字符与双对象都解析成功；截断 → `invalid_structure` + 片段；`generate` → `degraded=True`，`_generate_node` 写入 `failures`，`_resolve_answer` → `degraded=True`。
- 文档：book 里 `apps/api/main.py` / `paknow/gateway.py` / `paknow/chart.py` 的引用行号随代码移位更新；响应键清单与示例补上 `degraded` / `degradation`。
