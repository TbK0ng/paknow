"""Explicit, snapshot-pinned, in-process batch review for the loopback app."""
from __future__ import annotations

from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
import logging
from threading import Event, Lock, Thread
from typing import Callable

from paknow.pa_review import CLOUD_REVIEW_PARALLELISM, LOCAL_REVIEW_PARALLELISM

_LOG = logging.getLogger(__name__)
Work = Callable[[Event], dict]


class ReviewBatch:
    def __init__(self, batch_id: str, snapshot: tuple[int, int, int], jobs: list[tuple[int, Work]]) -> None:
        self.id, self.snapshot = batch_id, snapshot
        self._lock = Lock()
        self._stop = Event()
        self._queue = deque(jobs)
        self._items = {at: {"close_ms": at, "status": "queued"} for at, _ in jobs}
        self._bad_streak = 0
        self._reason: str | None = None
        self._done = False

    def start(self) -> None:
        Thread(target=self._work, name="pa-batch", daemon=True).start()

    def status(self) -> dict:
        with self._lock:
            items = [item.copy() for item in self._items.values()]
            return {"id": self.id, "snapshot": self.snapshot, "parallelism": LOCAL_REVIEW_PARALLELISM,
                    "cloud_parallelism": CLOUD_REVIEW_PARALLELISM,
                    "status": "stopped" if self._done and self._reason else "done" if self._done else
                    "stopping" if self._reason else "running", "stop_reason": self._reason,
                    "total": len(items), "counts": dict(Counter(i["status"] for i in items)),
                    "model_attempts": sum(i.get("model_called", False) for i in items), "items": items}

    def stop(self, reason: str = "manual") -> None:
        with self._lock:
            if not self._done:
                self._pause(reason)

    def _pause(self, reason: str) -> None:
        if self._reason is None:
            self._reason = reason
        self._stop.set()
        while self._queue:
            at, _ = self._queue.popleft()
            self._items[at]["status"] = "not_started"

    def _worker(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    return
                at, work = self._queue.popleft()
                self._items[at]["status"] = "running"
            try:
                result = work(self._stop)
            except Exception as exc:
                _LOG.warning("PA batch item failed (close_ms=%s, type=%s)", at, type(exc).__name__)
                result = {"status": "failed", "reason": "internal_error", "model_called": False}
            with self._lock:
                self._items[at].update(result)
                if result.get("reason") in {"provider_auth", "provider_billing", "provider_rate_limit", "inputs_changed"}:
                    self._pause(result["reason"])
                elif result.get("model_called"):
                    if result["status"] in {"approve", "wait", "reject"}:
                        self._bad_streak = 0
                    else:
                        self._bad_streak += 1
                        if self._bad_streak >= 5:
                            self._pause("consecutive_model_failures")

    def _work(self) -> None:
        try:
            with ThreadPoolExecutor(max_workers=LOCAL_REVIEW_PARALLELISM, thread_name_prefix="pa-review") as pool:
                tasks = [pool.submit(self._worker) for _ in range(LOCAL_REVIEW_PARALLELISM)]
                for task in tasks:
                    task.result()
        except Exception as exc:
            _LOG.warning("PA batch stopped (%s)", type(exc).__name__)
            self.stop("internal_error")
        finally:
            with self._lock:
                self._done = True


class BatchQueue:
    """One explicit batch at a time; repeat POSTs with its ID are reads."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._current: ReviewBatch | None = None

    def status(self) -> dict | None:
        with self._lock:
            return self._current.status() if self._current else None

    def submit(self, batch_id: str, snapshot: tuple[int, int, int], jobs: list[tuple[int, Work]]) -> dict:
        with self._lock:
            if self._current:
                last = self._current.status()
                if batch_id == self._current.id or last["status"] in {"running", "stopping"}:
                    return last
            self._current = ReviewBatch(batch_id, snapshot, jobs)
            self._current.start()
            return self._current.status()

    def stop(self, batch_id: str) -> dict | None:
        with self._lock:
            if self._current and self._current.id == batch_id:
                self._current.stop()
                return self._current.status()
        return None


batch_queue = BatchQueue()
