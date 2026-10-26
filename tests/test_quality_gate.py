"""Local quality gate: stratified sampling, hard-constraint exit codes, the
recorded noise floors, and baseline integrity. The chain is stubbed at the
module seam: no DB, no Qdrant, no model.
"""
from __future__ import annotations

import json

from scripts import quality_gate

STRATA = ("encyclopedia", "mechanism", "exception", "conditioned_claim")


# ---------------------------------------------------------------------------
# fixtures / fakes
# ---------------------------------------------------------------------------


def _dataset() -> dict:
    return json.loads(quality_gate.DEFAULT_DATASET.read_text(encoding="utf-8"))


def _det_record(item: dict, arm: str, *, coverage: float = 0.6, hard: bool = True) -> dict:
    return {
        "id": item["id"],
        "arm": arm,
        "category": item["category"],
        "status": "ok",
        "plan_source": "frozen",
        "citable_ids": ["e1"],
        "coverage": coverage,
        "hard_pass": hard,
        "hard_reasons": [] if hard else ["no_citable_evidence"],
    }


def _verdict(
    item: dict,
    arm: str,
    *,
    correctness: int = 2,
    condition_preservation: int = 2,
    citation_support: int = 2,
    claims: int = 0,
) -> dict:
    return {
        "id": item["id"],
        "arm": arm,
        "category": item["category"],
        "status": "ok",
        "hard_verdict": "pass",
        "hard_pass": True,
        "hard_reasons": [],
        "judged": True,
        "correctness": correctness,
        "condition_preservation": condition_preservation,
        "citation_support": citation_support,
        "unsupported_claims": ["claim"] * claims,
        "coverage": 1.0,
    }


class Runner:
    """Records for every sampled item, built from the items the gate passes in."""

    def __init__(self, *, coverage: float = 0.6, hard: bool = True):
        self.coverage = coverage
        self.hard = hard
        self.calls: list[list[dict]] = []

    def __call__(self, items):
        self.calls.append(list(items))
        return [
            _det_record(item, arm, coverage=self.coverage, hard=self.hard)
            for item in items
            for arm, _ in quality_gate.ARMS
        ]


class JudgeRunner:
    def __init__(self, *, correctness: int = 2, claims: int = 0):
        self.correctness = correctness
        self.claims = claims
        self.calls = 0

    def __call__(self, items):
        self.calls += 1
        return [
            _verdict(item, arm, correctness=self.correctness, claims=self.claims)
            for item in items
            for arm, _ in quality_gate.ARMS
        ]


def _sample() -> list[dict]:
    return quality_gate.stratified_sample(_dataset())


def _write_baseline(
    path,
    *,
    digest: str | None = None,
    blocks: dict | None = None,
    sample: list[str] | None = None,
) -> None:
    dataset = _dataset()
    payload = {
        "dataset": str(quality_gate.DEFAULT_DATASET),
        "dataset_sha256": digest or quality_gate.file_sha256(quality_gate.DEFAULT_DATASET),
        "recorded_at": "20260101T000000Z",
        "sample": sample if sample is not None else [i["id"] for i in _sample()],
    }
    if blocks:
        payload.update(blocks)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _baseline_block(coverage: float = 0.6, hard: bool = True) -> dict:
    return quality_gate.measure_deterministic(
        [_det_record(item, arm, coverage=coverage, hard=hard) for item in _sample() for arm, _ in quality_gate.ARMS]
    )


def _judge_block(correctness: int = 2, claims: int = 0) -> dict:
    return quality_gate.measure_judge(
        [_verdict(item, arm, correctness=correctness, claims=claims) for item in _sample() for arm, _ in quality_gate.ARMS]
    )


# ---------------------------------------------------------------------------
# sampling
# ---------------------------------------------------------------------------


def test_sample_is_ten_items_spread_over_the_four_strata():
    items = _sample()

    assert len(items) == 10
    assert [i["id"] for i in items] == [i["id"] for i in _sample()]
    counts = {stratum: sum(1 for i in items if i["category"] == stratum) for stratum in STRATA}
    assert all(2 <= n <= 3 for n in counts.values()), counts
    assert len({i["id"] for i in items}) == 10


def test_sample_strata_follow_the_dataset_order():
    items = _sample()
    order = [i["category"] for i in items]

    first = [order.index(stratum) for stratum in STRATA]
    assert first == sorted(first)


# ---------------------------------------------------------------------------
# hard constraints and exit codes
# ---------------------------------------------------------------------------


def test_a_run_matching_the_recorded_baseline_exits_zero(tmp_path, monkeypatch, capsys):
    baseline = tmp_path / "baseline.json"
    _write_baseline(baseline, blocks={"deterministic": _baseline_block()})
    monkeypatch.setattr(quality_gate, "run_deterministic", Runner())

    code = quality_gate.main(
        ["--baseline", str(baseline), "--out", str(tmp_path / "run")]
    )

    assert code == 0, capsys.readouterr().out


def test_the_deterministic_flag_is_the_explicit_default(tmp_path, monkeypatch, capsys):
    baseline = tmp_path / "baseline.json"
    _write_baseline(baseline, blocks={"deterministic": _baseline_block()})
    monkeypatch.setattr(quality_gate, "run_deterministic", Runner())

    code = quality_gate.main(
        ["--deterministic", "--baseline", str(baseline), "--out", str(tmp_path / "run")]
    )

    assert code == 0, capsys.readouterr().out


def test_a_hard_constraint_failure_exits_nonzero(tmp_path, monkeypatch, capsys):
    baseline = tmp_path / "baseline.json"
    _write_baseline(baseline, blocks={"deterministic": _baseline_block()})
    monkeypatch.setattr(quality_gate, "run_deterministic", Runner(hard=False))

    code = quality_gate.main(
        ["--baseline", str(baseline), "--out", str(tmp_path / "run")]
    )

    out = capsys.readouterr().out
    assert code == 1
    assert "no_citable_evidence" in out


def test_a_recorded_baseline_is_refused_when_the_run_fails(tmp_path, monkeypatch, capsys):
    baseline = tmp_path / "baseline.json"
    monkeypatch.setattr(quality_gate, "run_deterministic", Runner(hard=False))

    code = quality_gate.main(
        ["--record", "--baseline", str(baseline), "--out", str(tmp_path / "run")]
    )

    assert code == 1
    assert not baseline.exists()
    assert "refus" in capsys.readouterr().out.lower()


def test_recording_writes_the_dataset_hash_and_the_sample(tmp_path, monkeypatch):
    baseline = tmp_path / "baseline.json"
    monkeypatch.setattr(quality_gate, "run_deterministic", Runner())

    code = quality_gate.main(
        ["--record", "--baseline", str(baseline), "--out", str(tmp_path / "run")]
    )

    assert code == 0
    payload = json.loads(baseline.read_text(encoding="utf-8"))
    assert payload["dataset_sha256"] == quality_gate.file_sha256(quality_gate.DEFAULT_DATASET)
    assert payload["sample"] == [i["id"] for i in _sample()]
    assert payload["deterministic"]["arms"]["text"]["items"] == 10
    assert (tmp_path / "run" / "records.jsonl").exists()
    assert (tmp_path / "run" / "summary.json").exists()
    summary = json.loads((tmp_path / "run" / "summary.json").read_text(encoding="utf-8"))
    assert summary["answers_sha256"] == quality_gate.file_sha256(
        tmp_path / "run" / "records.jsonl"
    )


def test_recording_hashes_the_judge_records_too(tmp_path, monkeypatch):
    baseline = tmp_path / "baseline.json"
    monkeypatch.setattr(quality_gate, "run_deterministic", Runner())
    monkeypatch.setattr(quality_gate, "run_judge", JudgeRunner())

    code = quality_gate.main(
        ["--record", "--judge", "--baseline", str(baseline), "--out", str(tmp_path / "run")]
    )

    assert code == 0
    summary = json.loads((tmp_path / "run" / "summary.json").read_text(encoding="utf-8"))
    assert summary["answers_sha256"] == quality_gate.file_sha256(
        tmp_path / "run" / "records.jsonl"
    )
    assert summary["judge_sha256"] == quality_gate.file_sha256(
        tmp_path / "run" / "judge.records.jsonl"
    )


# ---------------------------------------------------------------------------
# soft floors
# ---------------------------------------------------------------------------


def test_a_coverage_drop_past_the_floor_exits_nonzero(tmp_path, monkeypatch, capsys):
    baseline = tmp_path / "baseline.json"
    _write_baseline(baseline, blocks={"deterministic": _baseline_block(coverage=0.60)})
    monkeypatch.setattr(quality_gate, "run_deterministic", Runner(coverage=0.50))

    code = quality_gate.main(
        ["--baseline", str(baseline), "--out", str(tmp_path / "run")]
    )

    assert code == 1
    assert "coverage_mean" in capsys.readouterr().out


def test_a_coverage_drop_inside_the_tolerance_exits_zero(tmp_path, monkeypatch, capsys):
    baseline = tmp_path / "baseline.json"
    _write_baseline(baseline, blocks={"deterministic": _baseline_block(coverage=0.60)})
    monkeypatch.setattr(quality_gate, "run_deterministic", Runner(coverage=0.58))

    code = quality_gate.main(
        ["--baseline", str(baseline), "--out", str(tmp_path / "run")]
    )

    assert code == 0, capsys.readouterr().out


def test_a_judge_score_drop_past_the_floor_exits_nonzero(tmp_path, monkeypatch, capsys):
    baseline = tmp_path / "baseline.json"
    _write_baseline(baseline, blocks={"deterministic": _baseline_block(), "judge": _judge_block()})
    monkeypatch.setattr(quality_gate, "run_deterministic", Runner())
    monkeypatch.setattr(quality_gate, "run_judge", JudgeRunner(correctness=1))

    code = quality_gate.main(
        ["--judge", "--baseline", str(baseline), "--out", str(tmp_path / "run")]
    )

    assert code == 1
    assert "correctness" in capsys.readouterr().out


def test_a_judge_score_drop_inside_the_tolerance_exits_zero(tmp_path, monkeypatch, capsys):
    baseline = tmp_path / "baseline.json"
    _write_baseline(baseline, blocks={"deterministic": _baseline_block(), "judge": _judge_block()})
    monkeypatch.setattr(quality_gate, "run_deterministic", Runner())
    # 2.0 -> 1.9 is a 0.1 drop, inside the 0.12 noise floor
    pairs = [(item, arm) for item in _sample() for arm, _ in quality_gate.ARMS]
    verdicts = [
        _verdict(item, arm, correctness=1 if index < 2 else 2)
        for index, (item, arm) in enumerate(pairs)
    ]
    monkeypatch.setattr(quality_gate, "run_judge", lambda items: verdicts)

    code = quality_gate.main(
        ["--judge", "--baseline", str(baseline), "--out", str(tmp_path / "run")]
    )

    assert code == 0, capsys.readouterr().out


def test_unsupported_claim_growth_past_the_floor_exits_nonzero(tmp_path, monkeypatch, capsys):
    baseline = tmp_path / "baseline.json"
    _write_baseline(baseline, blocks={"deterministic": _baseline_block(), "judge": _judge_block(claims=0)})
    monkeypatch.setattr(quality_gate, "run_deterministic", Runner())
    monkeypatch.setattr(quality_gate, "run_judge", JudgeRunner(claims=2))

    code = quality_gate.main(
        ["--judge", "--baseline", str(baseline), "--out", str(tmp_path / "run")]
    )

    # 10 judged records per arm x 2 claims = +20 total, past the +15 floor
    assert code == 1
    assert "unsupported_claims" in capsys.readouterr().out


def test_unsupported_claim_growth_inside_the_floor_exits_zero(tmp_path, monkeypatch, capsys):
    baseline = tmp_path / "baseline.json"
    _write_baseline(baseline, blocks={"deterministic": _baseline_block(), "judge": _judge_block(claims=0)})
    monkeypatch.setattr(quality_gate, "run_deterministic", Runner())
    monkeypatch.setattr(quality_gate, "run_judge", JudgeRunner(claims=1))

    code = quality_gate.main(
        ["--judge", "--baseline", str(baseline), "--out", str(tmp_path / "run")]
    )

    assert code == 0, capsys.readouterr().out


# ---------------------------------------------------------------------------
# baseline integrity
# ---------------------------------------------------------------------------


def test_a_dataset_hash_mismatch_refuses_the_baseline_before_running(tmp_path, monkeypatch, capsys):
    baseline = tmp_path / "baseline.json"
    _write_baseline(baseline, digest="0" * 64, blocks={"deterministic": _baseline_block()})
    runner = Runner()
    monkeypatch.setattr(quality_gate, "run_deterministic", runner)

    code = quality_gate.main(
        ["--baseline", str(baseline), "--out", str(tmp_path / "run")]
    )

    assert code == 1
    assert runner.calls == []
    assert "dataset_sha256" in capsys.readouterr().out


def test_a_different_sample_refuses_the_baseline(tmp_path, monkeypatch, capsys):
    baseline = tmp_path / "baseline.json"
    _write_baseline(
        baseline,
        blocks={"deterministic": _baseline_block()},
        sample=[f"old-{i}" for i in range(10)],
    )
    monkeypatch.setattr(quality_gate, "run_deterministic", Runner())

    code = quality_gate.main(
        ["--baseline", str(baseline), "--out", str(tmp_path / "run")]
    )

    assert code == 1
    assert "sample" in capsys.readouterr().out


def test_a_missing_baseline_is_refused_not_created(tmp_path, monkeypatch, capsys):
    baseline = tmp_path / "baseline.json"
    monkeypatch.setattr(quality_gate, "run_deterministic", Runner())

    code = quality_gate.main(
        ["--baseline", str(baseline), "--out", str(tmp_path / "run")]
    )

    assert code == 1
    assert not baseline.exists()


def test_judge_mode_needs_a_judge_block_in_the_baseline(tmp_path, monkeypatch, capsys):
    baseline = tmp_path / "baseline.json"
    _write_baseline(baseline, blocks={"deterministic": _baseline_block()})
    monkeypatch.setattr(quality_gate, "run_deterministic", Runner())
    monkeypatch.setattr(quality_gate, "run_judge", JudgeRunner())

    code = quality_gate.main(
        ["--judge", "--baseline", str(baseline), "--out", str(tmp_path / "run")]
    )

    assert code == 1
    assert "judge" in capsys.readouterr().out

