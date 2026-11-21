from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep

from paknow.pa_review import ModelSlots, ReviewLedger


def test_manual_local_admission_has_reserved_slot_without_increasing_paid_cap(tmp_path):
    ledger = ReviewLedger(tmp_path / "review.db", parallelism=1, manual_reserve=1)
    gate = ModelSlots(1)
    provider_started, manual_lookup, release = Event(), Event(), Event()
    order = []

    def run_bulk():
        with gate.take(manual=False):
            order.append("batch-start")
            provider_started.set()
            assert release.wait(3)
            order.append("batch-end")
        return {}

    def run_manual():
        manual_lookup.set()
        with gate.take(manual=True):
            order.append("manual-model")
        return {}

    with ThreadPoolExecutor(3) as pool:
        bulk = pool.submit(ledger.run, "bulk", run_bulk, queue=True)
        try:
            assert provider_started.wait(3)
            manual = pool.submit(ledger.run, "manual", run_manual)
            assert manual_lookup.wait(3)
            deadline = monotonic() + 2
            while monotonic() < deadline and not gate._manual_waiting:
                sleep(0.005)
            assert gate._manual_waiting == 1
            challenger = pool.submit(lambda: _background(gate, order))
            assert order == ["batch-start"]
        finally:
            release.set()
        bulk.result(3)
        manual.result(3)
        challenger.result(3)
    assert order == ["batch-start", "batch-end", "manual-model", "next-batch-model"]


def _background(gate, order):
    with gate.take(manual=False):
        order.append("next-batch-model")
