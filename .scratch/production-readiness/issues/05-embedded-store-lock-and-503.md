# 05: 内嵌向量库独占持有与 503

**What to build:** 内嵌向量库同一时刻只允许一个进程持有。第二个获取者立即失败而不是排队等待，并且失败信息说清是谁占着；这份占用原因要能穿透检索层的兜底捕获，让 API 明确回 503 与可读提示（"评测或脚本正在占用存储，请稍后重试"），而不是返回 200 加一屏空命中。

**Blocked by:** None (can start immediately)

**Status:** resolved

- [x] 客户端获取点加进程级独占锁（文件锁与存储目录同层）；第二个持有者立即失败，不阻塞等待。
- [x] 占用异常携带持锁进程信息，并有专门类型；检索层现有的 `except Exception` 兜底不得把它吞成失败清单后继续返回空答案。
- [x] API 捕获该异常后返回 503，提示文案说明"评估/脚本正在运行、可稍后重试"，不暴露堆栈。
- [x] 单进程内重复获取得到同一客户端，不重复构造。
- [x] 测试用可注入的锁路径模拟第二个持有者，不真的启动第二进程。
- [x] 外部向量库地址退路仍然可用（设置后不经过这把锁）。

## Comments

- `paknow/retrieval/qdrant_store.py`: `get_client` 在 `<objects_dir>/qdrant.lock` 上取 portalocker 独占锁（`LOCK_EX | LOCK_NB`，不等待），持锁身份（pid、取得时间、命令）写到同层 `qdrant.lock.holder`；`QDRANT_URL` 非空时整条锁路径跳过。Windows 的字节区间锁会让被锁文件对其他句柄不可读，所以身份放在旁边的文件而不是锁文件里。
- 新异常 `StoreLockedError(path, holder)`，消息即中文提示（"评测或脚本正在占用存储，请稍后重试"）。`pipeline._record_failure` 统一接管各步的 `except Exception`：锁冲突重新抛出，其余仍记入 `failures`。
- `apps/api/main.py` 注册 `StoreLockedError` 异常处理器 → `503 {"detail": <holder + 提示>}`，无堆栈。
- 测试：`tests/test_qdrant_store_lock.py`（注入锁路径持有第二把锁 → 立即报错且消息含 pid/命令；重复获取同一客户端；身份文件；server 绕过；持有者退出后可再取）、`tests/test_retrieval_pipeline.py::test_store_lock_conflict_is_not_swallowed_as_a_failure`、`tests/test_api_chat.py::test_store_lock_conflict_returns_503_with_a_readable_hint`。
- 真双进程 smoke：进程 A 拿真实嵌入式 client 持锁，进程 B 得到 `StoreLockedError`（pid/cmd 正确），经 API 走 `POST /query` 得到 503；A 退出后 B 可正常获取。全量 `pytest tests -q` 与 `scripts/check_scratch.py` 干净。
- 未做：把 qdrant-client 自带的 `RuntimeError("Storage folder ... already accessed ...")` 映射成同一异常（需按文案匹配，另一个 ticket 的启动自检更合适）。
