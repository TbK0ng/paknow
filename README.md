# PA Agent

在本地回放真实 OKX BTC-USDT 永续行情。PA 先标记候选，PAknow 再依据价格行为原文和信号前 80 根已收盘 K 线复核；可手动发起单笔或确认启动批量。只模拟成交，不接交易账户、不下实盘订单。

## 本地启动

在仓库根目录使用 Python 3.14，安装项目及依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

启动需要相互匹配且通过封存校验的 `data/paknow.db` 和 `data/objects/`。首次安装请按[建库说明](docs/pa-agent.md#首次建库)导入六份本地原件并封存。可从 `.env.example` 复制一份 `.env` 配置模型；不要提交密钥。

```powershell
.\.venv\Scripts\python.exe -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8765
```

打开 <http://127.0.0.1:8765/>，会直接进入 `/pa-agent`。选择「下个待复核」定位候选，再点「复核候选」，或确认「批量复核当前窗口」；播放、切换和刷新不会自动调用模型。到期或取消的方案可以从列表跳回原复核。

运行仓库检查前，用环境中的 Python 安装 `python -m pip install --group dev`（需要支持依赖组的 pip），再运行 `python scripts/check_scratch.py` 和 `python -m pytest -q`。

- [行情范围、回放规则、复核和排错](docs/pa-agent.md)
- [从原文到候选复核的取证流程](docs/pa-rag.md)
- [本地检索和离线维护](docs/agents/retrieval-dev.md)
- [上游规则版本与第三方许可](docs/licenses/pa-agent-notice.md)
