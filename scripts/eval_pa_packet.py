"""Offline PA packet diagnostic on confirmed, frozen OKX bars; no verdict or P&L."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
from time import perf_counter

from paknow.evidence.citations import citation_ready_hits
from paknow.graph.pa_context import ROLES, setup_at
from paknow.pa_market import SYMBOL, assemble
from paknow.pa_rules import Bar
from paknow.retrieval.pa_corpus import check_corpus, corpus
from paknow.retrieval.pa_packet import retrieve_packet
from paknow.retrieval.qdrant_store import check_store_ready, get_client


def evaluate(path: Path, cases: list[int]) -> dict:
    raw = path.read_bytes()
    snapshot = json.loads(raw)
    if snapshot["symbol"] != SYMBOL or snapshot["source"] != "okx" or any(
            b.get("closed") is not True for b in snapshot["bars"]):
        raise ValueError("expected a frozen confirmed OKX BTC-USDT perpetual snapshot")
    market = assemble([Bar(**{k: row[k] for k in Bar.__slots__}) for row in snapshot["bars"]], snapshot["downloaded_at_ms"])
    check_store_ready()
    check_corpus()
    generation, docs = corpus()
    if not docs:
        raise ValueError("seal the fresh PA corpus before evaluating")
    report = {"snapshot_sha256": sha256(raw).hexdigest(), "corpus": generation, "cases": []}
    for index, close_ms in enumerate(cases):
        state, bars = market.at(close_ms)
        if state["swing"]["status"] != "pending":
            raise ValueError(f"case {close_ms} is not a PA Original review candidate")
        setup = setup_at(state, bars)
        case = {"close_ms": close_ms, "setup": setup.payload(), "arms": {}}
        for graph in ((False, True) if index % 2 == 0 else (True, False)):
            start = perf_counter()
            p = retrieve_packet(setup, use_graph=graph)
            took = round((perf_counter() - start) * 1000, 1)
            if p.failures or p.generation != generation:
                raise ValueError(f"degraded evaluation: {p.failures}")
            ids = [s["id"] for s in p.sources]
            auth = {h["evidence_id"]: h for h in citation_ready_hits([{"evidence_id": eid} for eid in ids], doc_ids=docs)}
            assert len(ids) <= 5 and len(ids) == len(set(ids)) == len({s['passage_id'] for s in p.sources})
            for src in p.sources:
                start, end = src["span"]
                assert auth[src["id"]]["text"][start:end] == src["text"]
                assert src["provenance"] in {"native_text", "ocr"}
            verified = {s["role"] for s in p.sources if s["verified"]}
            assert p.gaps == [role for role in ROLES if role not in verified]
            case["arms"]["graph" if graph else "text"] = {
                "lookup_ms": took, "graph_status": p.graph_status, "gaps": p.gaps,
                "questions": p.questions, "sources": p.sources,
            }
        report["cases"].append(case)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("close_ms", nargs="+", type=int)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if args.out.exists() or args.out.resolve() == args.snapshot.resolve():
        parser.error("refusing to overwrite the snapshot or a prior result")
    try:
        report = evaluate(args.snapshot, args.close_ms)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        for case in report["cases"]:
            print(case["close_ms"], case["setup"]["side"], case["setup"]["zone"], case["setup"]["test"],
                  {name: {"gaps": arm["gaps"], "ms": arm["lookup_ms"]} for name, arm in case["arms"].items()})
    finally:
        if get_client.cache_info().currsize:
            get_client().close()


if __name__ == "__main__":
    main()
