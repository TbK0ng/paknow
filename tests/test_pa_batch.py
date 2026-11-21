from threading import Event
from time import monotonic, sleep

from paknow.pa_batch import BatchQueue
from paknow.pa_review import LOCAL_REVIEW_PARALLELISM


def test_provider_limit_stops_dispatch_but_drains_active_reviews():
    queue = BatchQueue()
    ready = [Event() for _ in range(LOCAL_REVIEW_PARALLELISM)]

    def limited(stop):
        ready[0].set()
        assert all(event.wait(3) for event in ready)
        return {"status": "failed", "reason": "provider_rate_limit", "model_called": True}

    def already_sent(i):
        def work(stop):
            ready[i].set()
            assert stop.wait(3)
            return {"status": "wait", "model_called": True}
        return work

    def forbidden(stop):
        raise AssertionError("a queued job dispatched after provider rate limit")

    jobs = [(0, limited)] + [(i, already_sent(i)) for i in range(1, LOCAL_REVIEW_PARALLELISM)]
    jobs += [(99, forbidden)]
    queue.submit("b", (1, 2, 3), jobs)
    deadline = monotonic() + 6
    while monotonic() < deadline:
        state = queue.status()
        if state["status"] == "stopped":
            break
        sleep(0.01)
    assert state["status"] == "stopped" and state["stop_reason"] == "provider_rate_limit"
    assert state["counts"] == {"failed": 1, "wait": LOCAL_REVIEW_PARALLELISM - 1, "not_started": 1}
    assert state["model_attempts"] == LOCAL_REVIEW_PARALLELISM
