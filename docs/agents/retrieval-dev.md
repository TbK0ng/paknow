# 本地 PA 证据库维护

PAknow 是 PA Agent 的内部取证模块。应用只启动已封存、相互匹配的 PA 纯文本 SQLite 和 Qdrant 索引。

| 内容 | 默认位置 | 覆盖项 |
|---|---|---|
| 权威证据数据库 | `data/paknow.db` | `PAKNOW_DB_URL` |
| Qdrant 和封存清单 | `data/objects/` | `OBJECTS_DIR` |
| 原件 | [六份源文件清单](../../configs/pa_corpus_sources.yaml) | 绝对路径或 `PAKNOW_CORPUS_DIR` 下的路径 |
| BGE-M3 和 BGE 重排模型 | 本地模型缓存 | `HF_HOME` |
| 真实行情快照、现役复核账本 | `data/market/` | 随仓库根目录 |

## 纯文本版的独立建库与封存

1. 准备文件名和 SHA-256 与清单匹配的六份本地原件。导入保留原文、图注和定位，不提取图片、不调用云模型。扫描件须先有可抽取的文本层。
2. 从仓库根目录在新终端配置全新的暂存位置，例如：

   ```powershell
   $stage = "./data/pa-passages-build-$(Get-Date -Format yyyyMMdd-HHmmss)"
   $env:PAKNOW_DB_URL = "sqlite:///$stage/paknow.db"
   $env:OBJECTS_DIR = "$stage/objects"
   ```

   建库使用嵌入式 Qdrant，`QDRANT_URL` 必须为空。嵌入式库同一时刻只允许一个进程访问。
3. 将六个路径都传给 `python -m scripts.ingest_all <原件1> ... <原件6>`，含空格的路径加引号。写库前先校验六份原件的文件名与哈希；通过后生成 BGE-M3 向量和 BM25，拒绝写入已有库。修改语料或术语词典需要重新建库，不能单独替换向量或清单。
4. 在同一暂存环境执行 `python -m scripts.seal_pa_corpus`。它核验源文件哈希、SQLite 与向量证据的逐点一致性，并用 `configs/pa_graph_sources.yaml` 建立 PA 审核过的取证关系。成功后写 `pa-corpus.json`，绑定构建版本、原文定位、父子关系及图谱来源。启动时核验封存、SQLite 摘要和 BM25 代次，不重复建库时的逐点对照。
5. 如需检验真实候选的取证行为，可在隔离库运行下面的离线工具。切库前停止服务，成对替换 SQLite 和对象目录；核对健康、行情和原文展开前保留前一对库用于回滚。以正常路径启动前清除暂存环境变量。

## 当前离线工具

- `python -m scripts.eval_pa_packet <真实OKX快照> <候选收盘毫秒...> --out <新结果文件>`：对照同一候选的文本与图谱证据包，不调用云模型、不生成交易。
- `python -m scripts.bench_pa_retrieval --help`：PA 专用本地取证耗时和缓存诊断。不要在服务持有的嵌入式库上运行。
- `python scripts/check_scratch.py`、`python -m pytest -q`：无需访问真实行情或调用付费模型的本地检查。

`retrieve_text` 为普通 K 线提供混合召回和相邻文本；`retrieve_packet` 为候选按三类问题规划和选证。两者的原文都由 `citation_ready_hits` 从 SQLite 核验。词法索引用术语词典和 jieba；BM25 的 `k1=1.2`、`b=0.75`，IDF 由 Qdrant 计算。图谱只服务取证，不是可引用事实。

## 维护边界

不要用 `git clean -fdX`。保留六份原件、`.env`、本地环境、模型缓存、真实行情、现役语料及现役代次的复核记录。备份或更换时，把 `data/paknow.db` 和 `data/objects/` 当作一套；不要手工删除 Qdrant 的锁文件或集合目录。
