# PA 原版规则的来源和许可

[`paknow/pa_rules.py`](../../paknow/pa_rules.py) 中的 PA 原版规则移植自 [`oficcejo/2pa-agent-rust`](https://github.com/oficcejo/2pa-agent-rust/tree/b6cf2d88380d7723d1e4c79249cf32465fbb690c)（规则版本 `2026-09-v1`）。Python 移植用于 PA Agent 对已收盘 K 线的本地分析；本仓库同时提供其源代码。PAknow 的证据复核和本地模拟是独立的应用层。

- 上游版权：Copyright (C) 2026 okx-2pa-agent-web Contributors。
- 许可标识：`AGPL-3.0-or-later`。见 [上游 LICENSE](upstream-LICENSE.txt) 与 [GNU AGPL v3 全文](AGPL-3.0.txt)。

## 交易方法的设计启发

[`paknow/pa_decision.py`](../../paknow/pa_decision.py) 的逐根决策卡参考了 Tom Hougaard 的《No Hindsight Price Action》所展示的逐根收盘、信号与跟进分离、条件情景和风险背景写法。该文章不是六份封存语料之一，项目没有复制其图表或正文，也不会把文章中的案例当作当前行情的可引用证据。
