"""Chart eval: three disjoint rate buckets, skipped semantics, record fields.

No model and no Qdrant: every chain call is a fake, and the judgement is pure.
"""
from __future__ import annotations

import pytest

from scripts import eval_chart

ITEM = {
    "id": "chart-01",
    "question": "图上哪些关键价位？",
    "image": "data/market/charts/chart-01.png",
    "image_sha256": "a" * 64,
    "expected_levels": [
        {"price": 7499.25, "source": "annotation", "strip": 1, "basis": "1 @ 7499.25"},
        {"price": 7489.15, "source": "annotation", "strip": 1, "basis": "2 @ 7489.15"},
    ],
    "expected_swings": [{"kind": "high", "price": 7502.5, "strip": 1, "basis": "前高"}],
    "expected_state": "trading_range",
    "unreadable": ["周期未标注"],
}


def _chart_read(*prices: float) -> dict:
    return {
        "market_state": "trading_range",
        "levels": [
            {"price": price, "source": "annotation", "strip": 1, "basis": "图上标注"}
            for price in prices
        ],
    }


# ---------------------------------------------------------------------------
# judgement: hit / miss / extra / drift
# ---------------------------------------------------------------------------


def test_exact_prices_are_hits_and_rates_are_zero():
    out = eval_chart.judge_levels([7499.25, 7489.15], [7499.25, 7489.15])

    assert out["matched_levels"] == [7499.25, 7489.15]
    assert out["missed_levels"] == []
    assert out["extra_levels"] == []
    assert out["drifted_levels"] == []
    assert (out["miss_rate"], out["extra_rate"], out["drift_rate"]) == (0.0, 0.0, 0.0)


def test_missing_expected_price_is_a_miss_and_never_an_extra():
    out = eval_chart.judge_levels([7499.25, 7489.15], [7499.25])

    assert out["matched_levels"] == [7499.25]
    assert out["missed_levels"] == [7489.15]
    assert out["extra_levels"] == []
    assert out["miss_rate"] == 0.5


def test_answered_price_without_expectation_is_extra():
    out = eval_chart.judge_levels([7499.25], [7499.25, 7510.5])

    assert out["matched_levels"] == [7499.25]
    assert out["extra_levels"] == [7510.5]
    assert out["extra_rate"] == 0.5
    assert out["missed_levels"] == []


def test_exact_hit_is_settled_before_drift_pairing():
    # observed 185.14 exactly hits one expectation; it must not drift to 185.0
    out = eval_chart.judge_levels([185.0, 185.14], [185.14])

    assert out["matched_levels"] == [185.14]
    assert out["missed_levels"] == [185.0]
    assert out["extra_levels"] == []
    assert out["drifted_levels"] == []


def test_last_digit_offset_is_drift_exclusive_of_hit_miss_and_extra():
    out = eval_chart.judge_levels([7499.25, 7489.15], [7499.75, 7489.75])

    assert out["drifted_levels"] == [
        {"expected": 7499.25, "observed": 7499.75},
        {"expected": 7489.15, "observed": 7489.75},
    ]
    assert out["matched_levels"] == []
    assert out["missed_levels"] == []
    assert out["extra_levels"] == []
    assert out["drift_rate"] == 1.0


def test_different_integer_part_is_not_drift():
    out = eval_chart.judge_levels([7499.25], [7500.25])

    assert out["drifted_levels"] == []
    assert out["missed_levels"] == [7499.25]
    assert out["extra_levels"] == [7500.25]


def test_rates_use_their_own_denominators():
    # 3 expected, 2 observed: 1 hit, 1 drift, 1 miss, 0 extra
    out = eval_chart.judge_levels([7499.25, 7489.15, 7500.0], [7499.75, 7500.0])

    assert out["matched_levels"] == [7500.0]
    assert out["drifted_levels"] == [{"expected": 7499.25, "observed": 7499.75}]
    assert out["missed_levels"] == [7489.15]
    assert out["miss_rate"] == round(1 / 3, 4)
    assert out["extra_rate"] == 0.0
    assert out["drift_rate"] == 0.5  # drifted / (matched + drifted)


def test_nearest_same_integer_part_candidate_wins_with_a_stable_tiebreak():
    out = eval_chart.judge_levels([7499.5], [7499.75, 7499.25])

    assert out["drifted_levels"] == [{"expected": 7499.5, "observed": 7499.25}]


def test_uncertain_observed_price_is_not_an_extra():
    read = _chart_read(7499.25)
    read["levels"].append({"price": None, "source": "unknown", "strip": 1, "basis": "看不清"})

    assert eval_chart.observed_prices(read) == [7499.25]


# ---------------------------------------------------------------------------
# run_item
# ---------------------------------------------------------------------------


def test_run_item_passes_the_image_and_reads_chart_read():
    seen: list[dict] = []

    def fake_run_query(query, images=None, *, expand_graph=True):
        seen.append({"query": query, "images": images, "expand_graph": expand_graph})
        return {"chart_read": _chart_read(7499.25, 7489.75), "chart_read_status": "structured"}

    record = eval_chart.run_item(ITEM, fake_run_query, image_loader=lambda item: (b"png", None))

    assert seen == [{"query": ITEM["question"], "images": [b"png"], "expand_graph": False}]
    assert record["status"] == "ok"
    assert record["observed_levels"] == [7499.25, 7489.75]
    assert record["matched_levels"] == [7499.25]
    assert record["drifted_levels"] == [{"expected": 7489.15, "observed": 7489.75}]
    assert record["chart_read_status"] == "structured"
    assert record["observed_state"] == "trading_range"
    assert record["expected_state"] == "trading_range"


def test_missing_image_skips_without_calling_the_chain():
    def boom(*_args, **_kwargs):
        raise AssertionError("chain must not run for a skipped item")

    record = eval_chart.run_item(
        ITEM, boom, image_loader=lambda item: (None, "image_missing")
    )

    assert record["status"] == "skipped"
    assert record["reason"] == "image_missing"
    assert record["chart_read"] is None
    assert record["matched_levels"] == [] and record["missed_levels"] == []


def test_default_loader_rejects_a_hash_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_chart, "ROOT", tmp_path)
    image = tmp_path / ITEM["image"]
    image.parent.mkdir(parents=True)
    image.write_bytes(b"not the frozen bytes")
    item = dict(ITEM, image=str(image.relative_to(tmp_path)).replace("\\", "/"))

    assert eval_chart.load_image(item) == (None, "image_hash_mismatch")
    assert eval_chart.load_image(dict(item, image="data/market/charts/missing.png")) == (
        None,
        "image_missing",
    )


def test_run_item_records_an_error_and_keeps_the_batch_alive():
    def boom(*_args, **_kwargs):
        raise RuntimeError("gateway down")

    record = eval_chart.run_item(ITEM, boom, image_loader=lambda item: (b"png", None))

    assert record["status"] == "error"
    assert "gateway down" in record["error"]
    assert record["matched_levels"] == []


# ---------------------------------------------------------------------------
# summary / resume
# ---------------------------------------------------------------------------


def _ok(item_id: str, expected: int, missed: int, extra: int, drift: int) -> dict:
    return {
        "id": item_id,
        "arm": "chart",
        "status": "ok",
        "elapsed_s": 1.0,
        "expected_levels": [0.0] * expected,
        "observed_levels": [0.0] * (expected - missed + extra),
        "matched_levels": [0.0] * (expected - missed - drift),
        "missed_levels": [0.0] * missed,
        "extra_levels": [0.0] * extra,
        "drifted_levels": [{"expected": 0.0, "observed": 0.0}] * drift,
    }


def test_summary_counts_skipped_separately_and_excludes_it_from_rates():
    records = [
        _ok("chart-01", expected=4, missed=1, extra=1, drift=1),
        {"id": "chart-02", "arm": "chart", "status": "skipped", "reason": "image_missing"},
        {"id": "chart-03", "arm": "chart", "status": "error", "error": "boom"},
    ]

    summary = eval_chart.summarize(records)

    assert (summary["items"], summary["ok"], summary["skipped"], summary["errors"]) == (3, 1, 1, 1)
    assert summary["skip_reasons"] == {"image_missing": 1}
    assert summary["expected_levels_total"] == 4
    assert summary["missed_total"] == 1 and summary["miss_rate"] == 0.25
    assert summary["answered_levels_total"] == 4
    assert summary["extra_total"] == 1 and summary["extra_rate"] == 0.25
    assert summary["drifted_total"] == 1 and summary["drift_rate"] == round(1 / 3, 4)


def test_pending_items_retries_errors_but_not_skips(tmp_path):
    path = tmp_path / "checkpoint.jsonl"
    eval_chart.append_checkpoint(path, _ok("chart-01", 1, 0, 0, 0))
    eval_chart.append_checkpoint(
        path, {"id": "chart-02", "arm": "chart", "status": "skipped", "reason": "image_missing"}
    )
    eval_chart.append_checkpoint(
        path, {"id": "chart-03", "arm": "chart", "status": "error", "error": "boom"}
    )
    checkpoint = eval_chart.load_checkpoint(path)

    todo = eval_chart.pending_items(
        [{"id": "chart-01"}, {"id": "chart-02"}, {"id": "chart-03"}, {"id": "chart-04"}],
        checkpoint,
    )

    assert [item["id"] for item in todo] == ["chart-03", "chart-04"]


def test_payload_details_list_prices_per_category():
    records = [
        {
            **_ok("chart-01", expected=2, missed=1, extra=0, drift=0),
            "missed_levels": [7499.25],
            "extra_levels": [],
            "drifted_levels": [],
        }
    ]

    payload = eval_chart.build_payload(
        dataset_path=eval_chart.DEFAULT_DATASET,
        digest="d" * 64,
        records=records,
        limit=1,
        timestamp="t",
    )

    assert payload["arms"] == {"chart": {"expand_graph": False}}
    assert payload["details"] == [
        {
            "id": "chart-01",
            "status": "ok",
            "reason": None,
            "missed_levels": [7499.25],
            "extra_levels": [],
            "drifted_levels": [],
        }
    ]
    assert payload["summary"]["miss_rate"] == 0.5
