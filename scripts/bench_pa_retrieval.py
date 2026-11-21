"""Compare serial and new local PA retrieval on an isolated sealed corpus. No model API or ledger."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
from statistics import median, quantiles
from threading import Event
from time import perf_counter
from unittest.mock import patch

from paknow.config import settings
from paknow.graph.pa_context import setup_at
from paknow.pa_market import SYMBOL, assemble
from paknow.pa_rules import Bar
from paknow.retrieval import embedding, search
from paknow.retrieval.pa_corpus import check_corpus, corpus
from paknow.retrieval.pa_packet import retrieve_packet
from paknow.retrieval.qdrant_store import check_store_ready, get_client
from paknow.retrieval.runtime import Priority, SingleFlightLRU, priority, runtime_stats


@contextmanager
def _serial_reference():
    # Same models, inference precision, cutoff, seeded questions and RRF configuration.
    with patch.object(search, "recall") as recall, \
            patch.object(search, "embed_query", lambda q: embedding._encode_queries([q])[0]), \
            patch.object(search, "score_pairs", search._predict_pairs):
        recall.get.side_effect = lambda _key, fn, urgency: fn(urgency)
        yield


def _retrieve(setup, *, manual: bool = False):
    token = priority.set(Priority(manual=manual))
    try:
        packet = retrieve_packet(setup)
        if packet.failures or packet.graph_status != "ok":
            raise RuntimeError(f"retrieval degraded: {packet.graph_status}, {packet.failures}")
        return packet
    finally:
        priority.reset(token)


def _load(snapshot: Path, closes: list[int]):
    if settings.objects_dir.resolve() == (settings.project_root / "data/objects").resolve() or settings.qdrant_url:
        raise ValueError("benchmark only an isolated embedded copy")
    raw = snapshot.read_bytes()
    saved = json.loads(raw)
    if saved["source"] != "okx" or saved["symbol"] != SYMBOL or not all(b.get('closed') is True for b in saved["bars"]):
        raise ValueError("expected a frozen, confirmed OKX market snapshot")
    tape = assemble([Bar(**{k: b[k] for k in Bar.__slots__}) for b in saved["bars"]], saved["downloaded_at_ms"])
    closes = closes or [state["close_ms"] for state in tape.states if state["swing"]["status"] == "pending"]
    inputs = [tape.at(at) for at in closes]
    if any(state["swing"]["status"] != "pending" for state, _ in inputs):
        raise ValueError("all closes must be PA Original candidates")
    setups = [setup_at(*item) for item in inputs]
    check_store_ready()
    check_corpus()
    generation, docs = corpus()
    if not docs:
        raise ValueError("isolated store must be sealed")
    # Warm model loading and kernels symmetrically, without warming the caches.
    import torch
    embedding._encode_queries(["price action"])
    search._predict_pairs([("price action", "a historical paragraph")])

    return generation, sha256(raw).hexdigest(), closes, setups


def evaluate(snapshot: Path, closes: list[int]) -> dict:
    generation, digest, closes, setups = _load(snapshot, closes)
    import torch

    def run(lookup):
        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
        start = perf_counter()
        results = lookup()
        elapsed = round((perf_counter() - start) * 1000, 1)
        peak = round(torch.cuda.max_memory_allocated() / 2**20, 1) if torch.cuda.is_available() else None
        return results, {"ms": elapsed, "peak_torch_mib": peak}

    def parallel():
        with ThreadPoolExecutor(max_workers=3) as pool:
            return list(pool.map(_retrieve, setups))

    with _serial_reference():
        baseline, serial = run(lambda: list(map(_retrieve, setups)))
    cold, cold_stats = run(parallel)
    warm, warm_stats = run(parallel)
    drift = [at for at, a, b, c in zip(closes, baseline, cold, warm, strict=True) if a != b or b != c]
    return {"corpus": generation, "snapshot_sha256": digest, "count": len(closes),
            "serial": serial, "parallel_cold_cache": cold_stats, "parallel_warm_cache": warm_stats,
            "different_packets_at": drift, "cache": runtime_stats()}


def sweep(snapshot: Path, closes: list[int], widths: list[int], repeats: int, manual_probe: bool = False) -> dict:
    generation, digest, closes, setups = _load(snapshot, closes)
    import torch
    cuda = torch.cuda.is_available()
    reference, results = None, {width: [] for width in widths}

    def timed(setup, manual=False, started=None):
        start = perf_counter()
        if started is not None:
            started.set()
        result = _retrieve(setup, manual=manual)
        return result, perf_counter() - start

    for round_no in range(repeats):
        for width in (widths if round_no % 2 == 0 else widths[::-1]):
            if cuda:
                torch.cuda.reset_peak_memory_stats()
            with patch.object(embedding, "query_embeddings", SingleFlightLRU(512)), \
                    patch.object(search, "recall", SingleFlightLRU(256)), \
                    patch.object(search, "rank_scores", SingleFlightLRU(8192)), \
                    ThreadPoolExecutor(width) as pool:
                start = perf_counter()
                manual_ms = None
                if manual_probe:
                    started = Event()
                    lead = pool.submit(timed, setups[0], False, started)
                    remaining = pool.map(timed, setups[1:])
                    if not started.wait(5):
                        raise RuntimeError("background retrieval did not start")
                    probe_index = len(setups) // 2
                    probe, probe_time = timed(setups[probe_index], manual=True)
                    manual_ms = round(probe_time * 1000)
                    rows = [lead.result(), *remaining]
                    if probe != rows[probe_index][0]:
                        raise RuntimeError("manual lookup differs from batch lookup")
                else:
                    rows = list(pool.map(timed, setups))
                elapsed = perf_counter() - start
            packets, times = map(list, zip(*rows))
            if reference is None:
                reference = packets
            drift = [at for at, received, expected in zip(closes, packets, reference, strict=True) if received != expected]
            p95 = quantiles(times, n=20, method="inclusive")[-1] if len(times) > 1 else times[0]
            peak = torch.cuda.max_memory_allocated() / 2**20 if cuda else None
            sample = {"seconds": round(elapsed, 3), "per_second": round(len(setups) / elapsed, 3),
                      "p95_ms": round(p95 * 1000), "peak_torch_mib": round(peak, 1) if peak is not None else None,
                      "peak_torch_reserved_mib": round(torch.cuda.max_memory_reserved() / 2**20, 1) if cuda else None,
                      "manual_ms": manual_ms, "different_packets_at": drift}
            results[width].append(sample)
            print(f"width={width} round={round_no + 1} {json.dumps(sample)}", flush=True)
    return {"corpus": generation, "snapshot_sha256": digest, "count": len(closes), "widths": {
        width: {"median_per_second": median(r["per_second"] for r in runs),
                "median_p95_ms": median(r["p95_ms"] for r in runs), "samples": runs} for width, runs in results.items()}}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("snapshot", type=Path)
    p.add_argument("closes", nargs="*", type=int)
    p.add_argument("--widths", nargs="+", type=int)
    p.add_argument("--repeats", default=2, type=int)
    p.add_argument("--manual-probe", action="store_true")
    args = p.parse_args()
    if args.widths and (args.repeats < 1 or any(n < 1 for n in args.widths)):
        p.error("positive widths and repeats are required")
    try:
        report = (sweep(args.snapshot, args.closes, args.widths, args.repeats, args.manual_probe)
                  if args.widths else evaluate(args.snapshot, args.closes))
        print(json.dumps(report, indent=2))
        if report.get("different_packets_at") or any(r["different_packets_at"] for w in report.get("widths", {}).values() for r in w["samples"]):
            raise SystemExit(1)
    finally:
        if get_client.cache_info().currsize:
            get_client().close()


if __name__ == "__main__":
    main()
