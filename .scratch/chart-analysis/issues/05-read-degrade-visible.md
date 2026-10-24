# 05: 读图失败的降级与可见标记

**What to build:** 读图超时、输出不是合法结构、或图片读不出内容时，回答仍按图表口径给出（继续读图，不做概念综述），同时明确告诉用户「这次未结构化读图」，并把失败写进本次运行的 `failures`。降级是看得见的，不是静默的。

**Blocked by:** 01

**Status:** resolved

- [ ] 读图抛错或结构非法时仍返回 200，回答仍按图表口径（先给图上价位，再给资料依据），不退化成知识问答
- [ ] 响应里有显式标记区分「结构化读图成功」与「未结构化读图」，`chart_read` 为 `null`
- [ ] 页面在该次回答上显示「未结构化读图」，样式区别于正常回答
- [ ] 本次运行 `failures` 至少有一条说明读图失败的原因类别（超时 / 非法结构 / 无内容），且不吞掉同一运行里的其他失败
- [ ] 降级路径不额外增加模型调用次数（总数与成功路径相同或更少）

## Comments

**Resolved** in `2ddb5d9`, merged as `6859c6e`；failure 落点随后由 07 移到读图节点，`failures` 仍恰好一条。

- 读图失败 → `chart_read=null`、`chart_read_status="unstructured"`，答案仍走图表口径（复审后走单阶段 `_CHART_FALLBACK_SYSTEM`），`failures` 记 `chart_read: <reason> (<detail>)`，不吞掉同一次运行的其他失败。
- 页面在 `isImage && status==='unstructured'` 时显示 `未结构化读图`（`.warn`）。
- 成功与降级路径调用次数相同（1 次读图 + 1 次合成）。
