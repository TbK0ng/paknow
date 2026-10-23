"""Blind answer judge: message blindness, strict parsing, hard-constraint
precedence and aggregation. Every model call is a stub; nothing here touches a
database, a network or Qdrant.
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from scripts import eval_judge


def _record(**overrides) -> dict:
    """An answer record as ``eval_answers`` writes it (grouping data included)."""
    record = {
        "id": "secret-id",
        "arm": "graph",
        "category": "secret-category",
        "question": "问：高开后出现三角形，突破失败之后通常怎么走？",
        "status": "ok",
        "answer": "高开后通常先进入整理，随后在三角形边缘等待突破。",
        "cited_evidence_ids": ["e1"],
        "citable_ids": ["e1", "e2"],
        "graph_evidence_ids": ["g1"],
        "routes": ["secret-route"],
        "rounds": 3,
        "failures": ["secret-failure"],
        "expected_evidence": ["e1"],
        "hard_pass": True,
        "invalid_citations": [],
        "graph_citations": [],
        "coverage": 1.0,
        "elapsed_s": 1.0,
    }
    record.update(overrides)
    return record


ITEM = {
    "id": "secret-id",
    "category": "secret-category",
    "question": "问：高开后出现三角形，突破失败之后通常怎么走？",
    "expected_evidence": ["e1"],
    "expected_points": [{"text": "高开后先进入整理。", "evidence_id": "e1"}],
}

EVIDENCE = {
    "e1": "跳空高开 + 交易区间 + 三角形：多头突破失败，随后进入交易区间。",
    "e2": "开盘即空头趋势。",
}


def _payload(**overrides) -> dict:
    payload = {
        "correctness": 2,
        "condition_preservation": 2,
        "citation_support": 1,
        "unsupported_claims": ["答案声称了证据未给出的目标位。"],
    }
    payload.update(overrides)
    return payload


def _ask(payload: dict, seen: list | None = None):
    """A faithful double for ``gateway.chat_json``: validate, then return."""

    def ask(schema, messages):
        if seen is not None:
            seen.append(messages)
        return schema.model_validate(payload)

    return ask


# ---------------------------------------------------------------------------
# message blindness
# ---------------------------------------------------------------------------


def test_messages_carry_no_arm_item_or_category():
    seen: list[list[dict]] = []
    text = _record(arm="text", category="encyclopedia", id="text-item")
    graph = _record(arm="graph", category="conditioned_claim", id="graph-item")
    item = dict(ITEM)

    eval_judge.judge_record(text, item, EVIDENCE, _ask(_payload(), seen))
    eval_judge.judge_record(graph, item, EVIDENCE, _ask(_payload(), seen))

    blob = json.dumps(seen, ensure_ascii=False)
    assert seen[0] == seen[1]  # same question/answer/evidence -> same prompt
    for leaked in ("text-item", "graph-item", "secret-category", "secret-route",
                   "secret-failure", "conditioned_claim", "encyclopedia"):
        assert leaked not in blob
    assert "e1" not in blob and "g1" not in blob  # evidence ids stay out
    assert "交易区间" in blob  # the evidence text itself is judge input


def test_messages_are_built_from_question_points_answer_and_evidence_only():
    seen: list[list[dict]] = []
    eval_judge.judge_record(_record(), ITEM, EVIDENCE, _ask(_payload(), seen))

    blob = json.dumps(seen, ensure_ascii=False)
    assert ITEM["question"] in blob
    assert "高开后先进入整理。" in blob
    assert "随后在三角形边缘等待突破" in blob
    assert EVIDENCE["e2"] not in blob  # only cited evidence is sent


# ---------------------------------------------------------------------------
# strict parsing
# ---------------------------------------------------------------------------


def test_strict_parse_rejects_a_missing_field():
    payload = _payload()
    del payload["unsupported_claims"]

    with pytest.raises(ValidationError):
        eval_judge.parse_scores(payload)


@pytest.mark.parametrize(
    "override",
    [
        {"correctness": 3},
        {"condition_preservation": -1},
        {"citation_support": "2"},
        {"correctness": 1.5},
        {"unsupported_claims": "没有"},
        {"extra": 1},
    ],
)
def test_strict_parse_rejects_out_of_range_or_mistyped_scores(override):
    with pytest.raises(ValidationError):
        eval_judge.parse_scores(_payload(**override))


def test_parse_scores_accepts_a_complete_object():
    scores = eval_judge.parse_scores(_payload())

    assert (scores.correctness, scores.condition_preservation) == (2, 2)
    assert scores.citation_support == 1
    assert scores.unsupported_claims == ["答案声称了证据未给出的目标位。"]


def test_a_failed_model_call_is_recorded_and_resumable():
    def ask(schema, messages):
        payload = _payload()
        del payload["citation_support"]
        return schema.model_validate(payload)

    record = eval_judge.judge_record(_record(), ITEM, EVIDENCE, ask)

    assert record["status"] == "error"
    assert record["judged"] is False
    assert record["correctness"] is None
    assert "citation_support" in record["error"]


def test_a_judged_record_carries_three_scores_and_the_unsupported_claims():
    record = eval_judge.judge_record(_record(), ITEM, EVIDENCE, _ask(_payload()))

    assert record["status"] == "ok"
    assert record["judged"] is True
    assert record["hard_verdict"] == "pass"
    assert record["correctness"] == 2
    assert record["condition_preservation"] == 2
    assert record["citation_support"] == 1
    assert record["unsupported_claims"] == ["答案声称了证据未给出的目标位。"]
    assert record["points_source"] == "dataset"
    assert "score" not in record  # the three scores are never merged


# ---------------------------------------------------------------------------
# hard-constraint precedence
# ---------------------------------------------------------------------------


def test_hard_constraint_fails_without_calling_the_model():
    def ask(schema, messages):
        raise AssertionError("the model must not be asked about a hard failure")

    record = eval_judge.judge_record(
        _record(cited_evidence_ids=["e9"]), ITEM, EVIDENCE, ask
    )

    assert record["status"] == "ok"
    assert record["hard_verdict"] == "fail"
    assert record["judged"] is False
    assert record["correctness"] is None
    assert record["hard_reasons"] == ["invalid_citations"]


def test_graph_only_citation_is_a_hard_failure():
    record = eval_judge.judge_record(
        _record(cited_evidence_ids=["e1", "g1"]), ITEM, EVIDENCE, _ask(_payload())
    )

    # a graph-only id is also outside the citable set, so both reasons fire
    assert record["hard_verdict"] == "fail"
    assert record["hard_reasons"] == ["invalid_citations", "graph_citations"]


def test_an_errored_answer_run_is_a_hard_failure():
    record = eval_judge.judge_record(
        _record(status="error", answer="", cited_evidence_ids=[]),
        ITEM,
        EVIDENCE,
        _ask(_payload()),
    )

    assert record["hard_verdict"] == "fail"
    assert "retrieval_error" in record["hard_reasons"]
    assert record["judged"] is False


# ---------------------------------------------------------------------------
# expected points
# ---------------------------------------------------------------------------


def test_items_without_authoring_points_fall_back_to_evidence_excerpts():
    item = {k: v for k, v in ITEM.items() if k != "expected_points"}

    points, source = eval_judge.expected_points(item, EVIDENCE)

    assert points == [EVIDENCE["e1"]]
    assert source == "evidence_excerpt"


def test_authoring_points_are_used_when_present():
    points, source = eval_judge.expected_points(ITEM, EVIDENCE)

    assert points == ["高开后先进入整理。"]
    assert source == "dataset"


# ---------------------------------------------------------------------------
# checkpoint / resume and sampling
# ---------------------------------------------------------------------------


def _judged_record(item_id: str, arm: str, *, status: str = "ok", hard: str = "pass") -> dict:
    return {
        "id": item_id,
        "arm": arm,
        "status": status,
        "hard_verdict": hard,
        "judged": status == "ok" and hard == "pass",
        "correctness": 2 if status == "ok" and hard == "pass" else None,
    }


def test_resume_retries_errors_and_keeps_hard_failures(tmp_path):
    path = tmp_path / "checkpoint.jsonl"
    eval_judge.append_checkpoint(path, _judged_record("q1", "text"))
    eval_judge.append_checkpoint(path, _judged_record("q1", "graph", hard="fail"))
    eval_judge.append_checkpoint(path, _judged_record("q2", "text", status="error"))
    checkpoint = eval_judge.load_checkpoint(path)
    order = [("q1", "text"), ("q1", "graph"), ("q2", "text"), ("q2", "graph")]

    pending = eval_judge.pending_keys(order, checkpoint)

    assert pending == [("q2", "text"), ("q2", "graph")]


def test_merge_keeps_answer_order_and_latest_verdict(tmp_path):
    path = tmp_path / "checkpoint.jsonl"
    eval_judge.append_checkpoint(path, _judged_record("q1", "text", status="error"))
    eval_judge.append_checkpoint(path, _judged_record("q2", "graph"))
    eval_judge.append_checkpoint(path, _judged_record("q1", "text"))
    checkpoint = eval_judge.load_checkpoint(path)
    order = [("q1", "text"), ("q1", "graph"), ("q2", "text"), ("q2", "graph")]

    merged = eval_judge.merge_judged(order, checkpoint)

    assert [r["id"] for r in merged] == ["q1", "q2"]
    assert merged[0]["status"] == "ok"


def test_sample_spreads_over_the_whole_batch_and_both_arms():
    order = [(f"i{n // 2}", "text" if n % 2 == 0 else "graph") for n in range(120)]

    picked = eval_judge.spread(order, 10)

    assert len(picked) == 10
    assert {arm for _, arm in picked} == {"text", "graph"}
    assert picked[0] == order[0] and picked[-1] == order[-1]


def test_sample_larger_than_the_batch_keeps_everything():
    order = [("q1", "text"), ("q1", "graph")]

    assert eval_judge.spread(order, 10) == order


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------


def _scored(
    item_id: str,
    arm: str,
    category: str,
    *,
    hard: str = "pass",
    scores: tuple[int, int, int] = (2, 2, 2),
    claims: tuple[str, ...] = (),
    status: str = "ok",
    coverage: float = 1.0,
) -> dict:
    judged = status == "ok" and hard == "pass"
    return {
        "id": item_id,
        "arm": arm,
        "category": category,
        "status": status,
        "hard_verdict": hard,
        "judged": judged,
        "correctness": scores[0] if judged else None,
        "condition_preservation": scores[1] if judged else None,
        "citation_support": scores[2] if judged else None,
        "unsupported_claims": list(claims),
        "coverage": coverage,
    }


def test_arm_summary_splits_hard_pass_rates_from_score_distributions():
    records = [
        _scored("a", "text", "encyclopedia", scores=(2, 2, 2)),
        _scored("b", "text", "encyclopedia", scores=(1, 0, 2), claims=("x", "y")),
        _scored("c", "text", "mechanism", hard="fail", coverage=0.0),
    ]

    summary = eval_judge.summarize_arm(records)

    assert summary["records"] == 3
    assert summary["hard_pass"] == 2
    assert summary["hard_pass_rate"] == 0.6667
    assert summary["judged"] == 2
    assert summary["scores"]["correctness"]["distribution"] == {"0": 0, "1": 1, "2": 1}
    assert summary["scores"]["correctness"]["mean"] == 1.5
    assert summary["scores"]["condition_preservation"]["mean"] == 1.0
    assert summary["unsupported_claims"]["total"] == 2
    assert summary["unsupported_claims"]["records_with_any"] == 1


def test_arm_summary_flags_full_support_with_listed_claims():
    records = [
        _scored("a", "text", "encyclopedia", scores=(2, 2, 2), claims=("x",)),
        _scored("b", "text", "encyclopedia", scores=(2, 2, 1), claims=("y",)),
        _scored("c", "text", "encyclopedia", scores=(2, 2, 2)),
    ]

    summary = eval_judge.summarize_arm(records)

    assert summary["claims_with_full_citation_support"] == 1


def test_aggregate_reports_every_stratum_and_arm():
    records = [
        _scored("a", "text", "encyclopedia", scores=(1, 1, 1)),
        _scored("a", "graph", "encyclopedia", scores=(2, 2, 2)),
        _scored("b", "text", "exception", hard="fail"),
        _scored("b", "graph", "exception", scores=(2, 1, 2), claims=("z",)),
    ]

    payload = eval_judge.aggregate(records)

    assert set(payload["by_stratum"]) == {"encyclopedia", "exception"}
    assert payload["by_stratum"]["encyclopedia"]["graph"]["scores"]["correctness"]["mean"] == 2.0
    assert payload["by_stratum"]["exception"]["text"]["hard_pass_rate"] == 0.0
    assert payload["counts"] == {
        "records": 4,
        "hard_pass": 3,
        "hard_fail": 1,
        "judged": 3,
        "errors": 0,
        "pending": 0,
    }
    assert payload["paired"]["n"] == 1
    assert payload["paired"]["correctness_mean_delta"] == 1.0
    assert payload["paired"]["better"] == 1
    assert payload["paired"]["hard_pass_text"] == 1
    assert payload["paired"]["hard_pass_graph"] == 2


def test_aggregate_counts_records_still_missing_from_the_checkpoint():
    records = [_scored("a", "text", "encyclopedia"), {"id": "a", "arm": "graph"}]

    payload = eval_judge.aggregate(records)

    assert payload["counts"]["pending"] == 1
    assert payload["by_arm"]["graph"]["records"] == 1
