# 06: API 启动自检

**What to build:** 服务启动时确认内嵌向量库可用：集合存在且可计数。存储缺失、被其它进程占用、集合为空三种情况分别给出人能读的原因并拒绝进入服务，避免出现"进程活着但回答永远是空"的半死状态。

**Blocked by:** 05

**Status:** resolved

- [x] 启动自检确认集合存在且可计数；三种失败（缺失 / 被占用 / 集合为空）各有独立、可读的原因，不共用一句泛化报错。
- [x] 自检不走会静默建集合的初始化路径，否则空库会被掩盖成"正常"。
- [x] 健康检查的语义明确：未就绪时返回非 ok 并带原因，或服务根本不接受请求——二者择一，不留下"ok 但不可用"。**取"服务根本不接受请求"**：未就绪时 lifespan 抛错，uvicorn 报 `Application startup failed` 并退出；因此 `/health` 保持纯存活探针，"活着"即"可用"。
- [x] 三种情况都有测试，且不依赖真实向量库、不启动第二进程。
- [x] 正常存储下现有接口行为与测试不受影响。
- [x] 额外：测试证明自检不创建集合（`created == []`），临时目录里跑完失败用例后集合列表仍为 `[]`。

## Comments

- `paknow/retrieval/qdrant_store.py`：新增只读探针 `check_store_ready()`（`collection_exists` + `count(exact=True)`，**不调用** `ensure_collections`，也就不会建集合）与 `StoreNotReadyError(reason, detail)`，`reason` ∈ `missing` / `empty` / `locked`，三者各有一句能直接照做的中文报错。`locked` 由 `StoreLockedError` 转换而来。
- 同一处补上 05 留下的空白：`get_client` 现在把 qdrant 自己的 `Storage folder … already accessed` 也翻成 `StoreLockedError`（持锁者从未取我们的锁文件时，这条消息是唯一证据），并在构造失败时释放我们自己的锁。调用方因此只需处理一种异常。
- `apps/api/main.py`：新增 `_lifespan`，启动时调 `check_store_ready()`。语义选"拒绝启动"而非"启动后 `/health` 非 ok"：半死服务的判别成本正是这条 ticket 要消掉的东西，而进程根本不起来时不存在 `ok` 但答不出的状态。`/health` 因此保持不变。
- `tests/test_api_startup_check.py`（8 个）：缺失 / 为空 / 被占用（同进程持锁 + 写 `.holder`，沿用 `test_qdrant_store_lock.py` 的做法）、三种原因不共用一句话、就绪时返回计数、不创建集合、qdrant 自身消息的翻译与锁释放、app 未就绪拒绝启动 / 就绪可启动。
- 文档：`book/chapter8-service.md` 新增"启动自检"小节并更正全部路由行号（插入 lifespan 后整体 +25），`README.md`、`docs/agents/retrieval-dev.md`（含 failure lookup 两行）同步。
- 冒烟（真实 uvicorn，临时 objects dir）：`missing` / `empty` / `locked` 三种均 `Application startup failed` 退出码 3，报错分别为 `向量库集合 evidence_text 不存在（…）`、`… 为空（…）`、`内嵌向量库已被占用（pid=4242, since=…, cmd=eval_text_arm.py）`；`ready` 目录（1 个 point）正常 `Application startup complete`，且失败用例的目录里集合列表仍为 `[]`。
- 全量 `pytest tests -q`：373 passed；`check_scratch.py`：31 file(s) OK。
