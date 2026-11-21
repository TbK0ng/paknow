"""Process-lifetime query caches and one bounded, priority-aware inference lane."""
from __future__ import annotations

from collections import Counter, OrderedDict
from concurrent.futures import Future
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Condition, Lock, Thread
from time import monotonic
from typing import Callable, Hashable, Sequence, TypeVar

T = TypeVar("T")


class Priority:
    def __init__(self, manual: bool = False) -> None:
        self._manual = manual
        self._parents: set[Priority] = set()
        self._lock = Lock()

    def follow(self, other: Priority) -> None:
        if self is not other:
            with self._lock:
                self._parents.add(other)

    @property
    def manual(self) -> bool:
        if self._manual:
            return True
        with self._lock:
            parents = tuple(self._parents)
        return any(p.manual for p in parents)


priority = ContextVar("pa_local_inference_priority", default=Priority(manual=True))


@dataclass
class _Flight:
    result: Future = field(default_factory=Future)
    urgency: Priority = field(default_factory=Priority)


class SingleFlightLRU:
    """A hit lasts across review batches; simultaneous misses share one result."""

    def __init__(self, maxsize: int) -> None:
        self.maxsize = maxsize
        self._lock = Lock()
        self._ready: OrderedDict[Hashable, object] = OrderedDict()
        self._flying: dict[Hashable, _Flight] = {}
        self._counts: Counter = Counter()

    def get(self, key: Hashable, fn: Callable[[Priority], T], urgency: Priority) -> T:
        with self._lock:
            if key in self._ready:
                self._ready.move_to_end(key)
                self._counts["hits"] += 1
                return self._ready[key]
            flight = self._flying.get(key)
            owner = flight is None
            if owner:
                flight = self._flying[key] = _Flight()
            flight.urgency.follow(urgency)
            self._counts["misses" if owner else "joined"] += 1
        if not owner:
            return flight.result.result()
        try:
            result = fn(flight.urgency)
        except BaseException as exc:
            with self._lock:
                self._flying.pop(key)
            flight.result.set_exception(exc)
            raise
        with self._lock:
            self._ready[key] = result
            if len(self._ready) > self.maxsize:
                self._ready.popitem(last=False)
                self._counts["evictions"] += 1
            self._flying.pop(key)
        flight.result.set_result(result)
        return result

    def get_many(self, keys: list[Hashable], fn: Callable[[list[Hashable], Priority], list[T]],
                 urgency: Priority) -> list[T]:
        """Reserve missing keys atomically and compute our misses before waiting on others."""
        futures, owned = {}, {}
        group = Priority()
        with self._lock:
            for key in dict.fromkeys(keys):
                if key in self._ready:
                    self._ready.move_to_end(key)
                    self._counts["hits"] += 1
                    fut = Future()
                    fut.set_result(self._ready[key])
                    futures[key] = fut
                    continue
                flight = self._flying.get(key)
                if flight is None:
                    flight = self._flying[key] = owned[key] = _Flight()
                    group.follow(flight.urgency)
                    self._counts["misses"] += 1
                else:
                    self._counts["joined"] += 1
                flight.urgency.follow(urgency)
                futures[key] = flight.result
        if owned:
            try:
                values = list(fn(list(owned), group))
                if len(values) != len(owned):
                    raise ValueError("cache result count does not match keys")
            except BaseException as exc:
                with self._lock:
                    for key in owned:
                        self._flying.pop(key)
                for flight in owned.values():
                    flight.result.set_exception(exc)
                raise
            with self._lock:
                for key, value in zip(owned, values, strict=True):
                    self._ready[key] = value
                    self._flying.pop(key)
                    if len(self._ready) > self.maxsize:
                        self._ready.popitem(last=False)
                        self._counts["evictions"] += 1
            for flight, value in zip(owned.values(), values, strict=True):
                flight.result.set_result(value)
        return [futures[key].result() for key in keys]

    def stats(self) -> dict:
        with self._lock:
            return {**self._counts, "size": len(self._ready), "in_flight": len(self._flying), "capacity": self.maxsize}


@dataclass(eq=False)
class _Job:
    kind: str
    runner: Callable
    parts: tuple
    urgency: Priority
    expires: float
    values: list = field(default_factory=list)
    future: Future = field(default_factory=Future)


class InferenceLane:
    """A single GPU/CPU-model owner; combine only same-kind, same-priority work."""

    def __init__(self, limits: dict[str, int], coalesce_ms: float = 3.0) -> None:
        self.limits = limits.copy()
        self._delay = coalesce_ms / 1000
        self._condition = Condition()
        self._jobs: list[_Job] = []
        self._thread: Thread | None = None
        self._closed = False
        self._counts: Counter = Counter()

    def call(self, kind: str, parts: Sequence, runner: Callable, urgency: Priority) -> tuple:
        if not parts:
            return ()
        if kind not in self.limits or self.limits[kind] < 1:
            raise ValueError(f"unknown inference kind: {kind}")
        job = _Job(kind, runner, tuple(parts), urgency, monotonic() + self._delay)
        with self._condition:
            if self._closed:
                raise RuntimeError("inference lane is closed")
            self._jobs.append(job)
            if self._thread is None:
                self._thread = Thread(target=self._work, name="pa-local-models", daemon=True)
                self._thread.start()
            self._condition.notify()
        return job.future.result()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join()

    def stats(self) -> dict:
        with self._condition:
            return {**self._counts, "queued_jobs": len(self._jobs), "limits": self.limits.copy()}

    def _take(self) -> list[tuple[_Job, tuple]]:
        with self._condition:
            while True:
                if not self._jobs:
                    if self._closed:
                        return []
                    self._condition.wait()
                    continue
                jobs = [(job, job.urgency.manual) for job in self._jobs]
                first, manual = next(((job, urgent) for job, urgent in jobs if urgent), jobs[0])
                group = [job for job, urgent in jobs if job.kind == first.kind and job.runner is first.runner
                         and urgent == manual]
                room = self.limits[first.kind]
                if not manual and sum(len(j.parts) for j in group) < room and (wait := first.expires - monotonic()) > 0:
                    self._condition.wait(wait)
                    continue
                batch = []
                for job in group:
                    n = min(room, len(job.parts))
                    batch.append((job, job.parts[:n]))
                    job.parts = job.parts[n:]
                    self._jobs.remove(job)
                    if job.parts:
                        self._jobs.append(job)
                    room -= n
                    if not room:
                        break
                self._counts[first.kind + "_batches"] += 1
                self._counts[first.kind + "_items"] += sum(len(xs) for _, xs in batch)
                self._counts[first.kind + "_mixed_batches"] += len(batch) > 1
                return batch

    @staticmethod
    def _evaluate(job: _Job, parts: list) -> list:
        values = list(job.runner(parts))
        if len(values) != len(parts):
            raise ValueError("inference result count does not match inputs")
        return values

    def _finish(self, job: _Job, values: list | None, error: BaseException | None = None) -> None:
        if error is not None:
            with self._condition:
                if job in self._jobs:
                    self._jobs.remove(job)
                self._counts["failed_jobs"] += 1
            job.future.set_exception(error)
            return
        job.values.extend(values)
        if not job.parts:
            job.future.set_result(tuple(job.values))

    def _work(self) -> None:
        while batch := self._take():
            try:
                values = self._evaluate(batch[0][0], [part for _, parts in batch for part in parts])
            except BaseException as error:
                if len(batch) == 1:
                    self._finish(batch[0][0], None, error)
                    continue
                # Isolate a malformed/oversized job before reporting partial evidence.
                for job, parts in batch:
                    try:
                        local = self._evaluate(job, list(parts))
                    except BaseException as failure:
                        self._finish(job, None, failure)
                    else:
                        self._finish(job, local)
                continue
            start = 0
            for job, parts in batch:
                end = start + len(parts)
                self._finish(job, values[start:end])
                start = end


inference = InferenceLane({"embed": 8, "rerank": 6})
query_embeddings = SingleFlightLRU(512)
recall = SingleFlightLRU(256)
rank_scores = SingleFlightLRU(8192)
qdrant_reads = Lock()


def runtime_stats() -> dict:
    return {"query_embeddings": query_embeddings.stats(), "recall": recall.stats(),
            "rerank": rank_scores.stats(), "inference": inference.stats()}
