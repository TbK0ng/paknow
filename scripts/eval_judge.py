"""Blind answer judge over the committed answer-level batch.

The judge consumes ``eval_answers.py``'s ``records.jsonl`` and the v2 gold
dataset, and produces one verdict per (item, arm) record:

1. **Hard constraints first, deterministically.** A record whose answer run
   errored, whose answer is empty, or whose stored citations violate the
   citation constraints (out-of-set id, graph-only id) is a ``fail`` verdict and
   is never sent to the model.
2. **One blind model call otherwise.** The message carries only the question,
   the expected answer points, the answer text and the text of the evidence the
   answer cited (fetched read-only from the metadata DB by id). The arm, the
   item id, the stratum, the routes, the rounds and the retrieval failures are
   never in the message, so the judge cannot tell which arm produced the answer.
3. **Strict structured output.** One ``gateway.chat_json`` call against
   :class:`JudgeScores`: three integers 0-2 (correctness, condition
   preservation, citation support) plus an ``unsupported_claims`` list. A
   missing field, an out-of-range value, a wrong type or an extra key fails the
   record; a failed call is retried on ``--resume``.

The three scores are diagnostics and ordering aids; they are never merged into
one number, and the deterministic hard verdict stays the verdict.

Aggregation is by stratum and by arm: hard-pass rate, per-score distributions,
unsupported-claim counts and expected-evidence coverage, with every per-record
verdict retained. Checkpointing is per (item, arm), so an interrupted run
resumes without re-paying for finished records.

Expected answer points: the 40 new v2 items carry authored ``expected_points``
bound to evidence ids. The 20 original items ship unchanged and have none; for
those the expected-evidence text is used as the correctness anchor and the
record is marked ``points_source: "evidence_excerpt"``.

Usage:
    python scripts/eval_judge.py --sample 10 --out .scratch/retrieval-eval-parity/judgements/spot
    python scripts/eval_judge.py --out .scratch/retrieval-eval-parity/judgements/<timestamp>
    python scripts/eval_judge.py --resume --out <same dir as the interrupted run>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.eval_answers import (  # noqa: E402
    ANSWERS_DIR,
    DEFAULT_DATASET,
    refresh_checks,
)

DEFAULT_ANSWERS = ANSWERS_DIR / "20261023T104318Z"
JUDGEMENTS_DIR = ROOT / ".scratch" / "retrieval-eval-parity" / "judgements"

ARMS = ("text", "graph")
SCORE_KEYS = ("correctness", "condition_preservation", "citation_support")
SCORE_BUCKETS = ("0", "1", "2")

#: per-evidence cap on what reaches the judge; the full text stays in the DB
EVIDENCE_CHAR_CAP = 1200
MISSING_TEXT = "（证据原文无法取得）"

#: ``chat_json`` shares max_tokens between reasoning and content; this judge's
#: reasoning runs to 8k tokens on the longest answers, and at 8192 the content
#: came back empty at finish=length
JUDGE_MAX_TOKENS = 16384

SYSTEM_PROMPT = """\
你是答案质量判定器。只依据用户消息里给出的题面、期望答案要点、待判答案与引用证据文本判断，
不要引入外部知识，不要推测答案由谁或由哪条链路产生。

三个维度各给一个 0-2 的整数分：
- correctness 结论正确：0 = 与要点冲突或没有回答；1 = 只答到部分要点；2 = 覆盖全部要点且无实质错误。
- condition_preservation 条件保留：0 = 要点里的条件被漏掉或被改写成无条件结论；
  1 = 条件只保留了一部分；2 = 条件与语气完整保留。要点本身没有条件时，答案也不得自行添加无依据的条件或限定，
  否则记 0-1。
- citation_support 引用支撑：0 = 引用缺位，或所引证据不支持主要结论；1 = 只有部分主要结论被引用证据支撑；
  2 = 每条主要结论都能由所引证据文本推出。

unsupported_claims：逐条列出答案中无法由所引证据文本推出的**事实性断言**（短句，不要编号）；
答案明确说明「资料未给出 / 未检索到 / 无法量化」这类关于证据范围的陈述不算不支持断言
（只有当证据确实给出了该内容时才算错误）。全部有支撑时给空数组。

citation_support 必须与 unsupported_claims 一致：unsupported_claims 非空时 citation_support 最多为 1；
若列出的断言包含主要结论，citation_support 记 0。
先在心里核对，然后直接输出 JSON：不要复述题面、不要逐条分析、不要给出总体结论或单一总分。\
"""


class JudgeScores(BaseModel):
    """Strict judge output: exactly the three scores plus unsupported claims."""

    model_config = ConfigDict(extra="forbid")

    correctness: int = Field(ge=0, le=2, strict=True)
    condition_preservation: int = Field(ge=0, le=2, strict=True)
    citation_support: int = Field(ge=0, le=2, strict=True)
    unsupported_claims: list[str]


Ask = Callable[[type[BaseModel], list[dict[str, str]]], Any]


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------


def load_records(path: Path) -> list[dict]:
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        records.append(json.loads(line))
    return records


def load_items(path: Path) -> dict[str, dict]:
    dataset = json.loads(path.read_text(encoding="utf-8"))
    return {str(item["id"]): item for item in dataset["items"]}


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_evidence_texts(ids: Iterable[str]) -> dict[str, str]:
    """Read evidence text by id. Read-only: one ``SELECT``, no writes."""
    wanted = sorted({str(i) for i in ids if i})
    if not wanted:
        return {}
    from paknow.db import SessionLocal
    from paknow.db.models import EvidenceUnitRow

    texts: dict[str, str] = {}
    with SessionLocal() as session:
        rows = (
            session.query(EvidenceUnitRow.evidence_id, EvidenceUnitRow.text)
            .filter(EvidenceUnitRow.evidence_id.in_(wanted))
            .all()
        )
        for evidence_id, text in rows:
            texts[str(evidence_id)] = text or ""
    return texts


def clip(text: str, limit: int = EVIDENCE_CHAR_CAP) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…"


# ---------------------------------------------------------------------------
# what the judge is allowed to see
# ---------------------------------------------------------------------------


def expected_points(
    item: Mapping[str, Any], evidence_texts: Mapping[str, str]
) -> tuple[list[str], str]:
    """Authored answer points, else the expected-evidence text as the anchor."""
    authored = item.get("expected_points") or []
    if authored:
        return [str(p["text"]) for p in authored], "dataset"
    points = [
        clip(evidence_texts.get(str(eid), MISSING_TEXT))
        for eid in item.get("expected_evidence") or []
    ]
    return points, "evidence_excerpt"


def blind_messages(
    *,
    question: str,
    points: Sequence[str],
    points_source: str,
    answer: str,
    cited_texts: Sequence[str],
) -> list[dict[str, str]]:
    """The judge's whole input: question, points, answer, cited evidence text.

    No arm, item id, stratum, route, round or failure ever reaches this
    function, so it cannot leak into the message.
    """
    if points_source == "dataset":
        points_head = "# 期望答案要点（人工撰写，逐条绑定来源证据）"
    else:
        points_head = (
            "# 期望答案要点（由期望证据原文摘录，可能只是标题或短语；"
            "答案覆盖其含意即可，不要求复述原文）"
        )
    points_block = "\n".join(f"- {p}" for p in points) or "（无）"
    evidence_block = "\n\n---\n\n".join(clip(t) for t in cited_texts) or "（答案未引用证据）"
    user = (
        f"# 题面\n{question}\n\n"
        f"{points_head}\n{points_block}\n\n"
        f"# 待判答案\n{answer}\n\n"
        "# 答案引用的证据原文（集合，顺序无意义；编号与答案中的 [n] 不对应）\n"
        f"{evidence_block}"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def hard_constraints(record: Mapping[str, Any]) -> list[str]:
    """Deterministic reasons a record is a fail verdict, recomputed from ids."""
    reasons: list[str] = []
    if record.get("status") != "ok":
        reasons.append("retrieval_error")
    if not str(record.get("answer") or "").strip():
        reasons.append("empty_answer")
    checks = refresh_checks(record)
    if checks.get("invalid_citations"):
        reasons.append("invalid_citations")
    if checks.get("graph_citations"):
        reasons.append("graph_citations")
    return reasons


def parse_scores(payload: Any) -> JudgeScores:
    """Strict parse: every field required, integers 0-2, no extras."""
    return JudgeScores.model_validate(payload)


# ---------------------------------------------------------------------------
# one verdict
# ---------------------------------------------------------------------------


def _blank_scores() -> dict[str, Any]:
    return {
        "correctness": None,
        "condition_preservation": None,
        "citation_support": None,
        "unsupported_claims": [],
    }


def judge_record(
    record: Mapping[str, Any],
    item: Mapping[str, Any],
    evidence_texts: Mapping[str, str],
    ask: Ask,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """One verdict: deterministic hard constraints first, one model call after."""
    started = clock()
    cited = [str(eid) for eid in record.get("cited_evidence_ids") or []]
    cited_texts = [evidence_texts.get(eid, MISSING_TEXT) for eid in cited]
    reasons = hard_constraints(record)
    verdict: dict[str, Any] = {
        "id": str(record["id"]),
        "arm": str(record.get("arm")),
        "category": record.get("category"),
        "status": "ok",
        "hard_verdict": "fail" if reasons else "pass",
        "hard_reasons": reasons,
        "judged": False,
        "points_source": None,
        "citation_count": len(cited),
        "evidence_units": sum(1 for t in cited_texts if t != MISSING_TEXT),
        "evidence_missing": sum(1 for t in cited_texts if t == MISSING_TEXT),
        "evidence_chars": sum(len(clip(t)) for t in cited_texts),
        "coverage": float(record.get("coverage") or 0.0),
        "error": None,
        **_blank_scores(),
    }
    if reasons:
        verdict["elapsed_s"] = round(clock() - started, 3)
        return verdict

    points, source = expected_points(item, evidence_texts)
    messages = blind_messages(
        question=str(record.get("question") or item.get("question") or ""),
        points=points,
        points_source=source,
        answer=str(record.get("answer") or ""),
        cited_texts=cited_texts,
    )
    try:
        raw = ask(JudgeScores, messages)
        scores = raw if isinstance(raw, JudgeScores) else parse_scores(raw)
    except Exception as e:  # noqa: BLE001 - any parse/call failure is a record
        verdict["status"] = "error"
        verdict["error"] = f"{type(e).__name__}: {e}"
        verdict["points_source"] = source
        verdict["elapsed_s"] = round(clock() - started, 3)
        return verdict

    verdict.update(
        judged=True,
        points_source=source,
        correctness=int(scores.correctness),
        condition_preservation=int(scores.condition_preservation),
        citation_support=int(scores.citation_support),
        unsupported_claims=list(scores.unsupported_claims),
        elapsed_s=round(clock() - started, 3),
    )
    return verdict


# ---------------------------------------------------------------------------
# checkpoint / resume
# ---------------------------------------------------------------------------


def record_key(record: Mapping[str, Any]) -> tuple[str, str]:
    return (str(record["id"]), str(record["arm"]))


def load_checkpoint(path: Path) -> dict[tuple[str, str], dict]:
    """Latest verdict per (item, arm); the last line wins."""
    latest: dict[tuple[str, str], dict] = {}
    if not path.exists():
        return latest
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            # a killed process may leave a partial trailing line
            continue
        if "id" not in record or "arm" not in record:
            continue
        latest[record_key(record)] = record
    return latest


def append_checkpoint(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()


def pending_keys(
    order: Sequence[tuple[str, str]], checkpoint: Mapping[tuple[str, str], dict]
) -> list[tuple[str, str]]:
    """Records to judge: missing, plus those whose last call errored."""
    return [
        key for key in order
        if key not in checkpoint or checkpoint[key].get("status") != "ok"
    ]


def merge_judged(
    order: Sequence[tuple[str, str]], checkpoint: Mapping[tuple[str, str], dict]
) -> list[dict]:
    """Answer-batch order, latest verdict per key."""
    return [checkpoint[key] for key in order if key in checkpoint]


def spread(seq: Sequence[Any], n: int) -> list[Any]:
    """``n`` items spread evenly over ``seq`` (both ends included)."""
    if n <= 0:
        return list(seq)
    if n >= len(seq):
        return list(seq)
    if n == 1:
        return [seq[0]]
    last = len(seq) - 1
    return [seq[round(i * last / (n - 1))] for i in range(n)]


# ---------------------------------------------------------------------------
# aggregation (pure)
# ---------------------------------------------------------------------------


def _score_block(judged: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    values = [int(r[key]) for r in judged if r.get(key) is not None]
    buckets = Counter(str(v) for v in values)
    return {
        "mean": round(sum(values) / len(values), 4) if values else None,
        "distribution": {name: buckets.get(name, 0) for name in SCORE_BUCKETS},
    }


def summarize_arm(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Hard-pass rate over evaluated records; score stats over judged records."""
    decided = [r for r in records if r.get("hard_verdict") in ("pass", "fail")]
    hard_pass = sum(1 for r in decided if r["hard_verdict"] == "pass")
    judged = [r for r in records if r.get("judged")]
    claims = [len(r.get("unsupported_claims") or []) for r in judged]
    coverage = [float(r["coverage"]) for r in records if r.get("coverage") is not None]
    return {
        "records": len(records),
        "hard_pass": hard_pass,
        "hard_fail": len(decided) - hard_pass,
        "hard_pass_rate": round(hard_pass / len(decided), 4) if decided else 0.0,
        "judged": len(judged),
        "errors": sum(1 for r in records if r.get("status") == "error"),
        "pending": sum(1 for r in records if r.get("status") not in ("ok", "error")),
        "scores": {key: _score_block(judged, key) for key in SCORE_KEYS},
        "unsupported_claims": {
            "total": sum(claims),
            "mean": round(sum(claims) / len(claims), 4) if claims else None,
            "records_with_any": sum(1 for n in claims if n),
        },
        "claims_with_full_citation_support": sum(
            1
            for r in judged
            if r.get("unsupported_claims") and int(r.get("citation_support") or 0) == 2
        ),
        "coverage_mean": round(sum(coverage) / len(coverage), 4) if coverage else None,
    }


def paired_delta(records: Sequence[Mapping[str, Any]], arms: Sequence[str] = ARMS) -> dict:
    """Arm-to-arm deltas on records that have both arms."""
    by_key = {(r["id"], r.get("arm")): r for r in records}
    ids = list(dict.fromkeys(r["id"] for r in records))
    pairs = [
        (by_key[(i, arms[0])], by_key[(i, arms[1])])
        for i in ids
        if (i, arms[0]) in by_key and (i, arms[1]) in by_key
    ]
    scored = [(a, b) for a, b in pairs if a.get("judged") and b.get("judged")]

    def mean_delta(key: str) -> float | None:
        deltas = [int(b[key]) - int(a[key]) for a, b in scored]
        return round(sum(deltas) / len(deltas), 4) if deltas else None

    better = sum(1 for a, b in scored if b["correctness"] > a["correctness"])
    worse = sum(1 for a, b in scored if b["correctness"] < a["correctness"])
    return {
        "n": len(scored),
        "correctness_mean_delta": mean_delta("correctness"),
        "condition_preservation_mean_delta": mean_delta("condition_preservation"),
        "citation_support_mean_delta": mean_delta("citation_support"),
        "better": better,
        "worse": worse,
        "equal": len(scored) - better - worse,
        "hard_pass_text": sum(1 for a, _ in pairs if a.get("hard_verdict") == "pass"),
        "hard_pass_graph": sum(1 for _, b in pairs if b.get("hard_verdict") == "pass"),
        "paired_items": len(pairs),
    }


def aggregate(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Counts, per-arm and per-stratum summaries, paired deltas."""
    decided = [r for r in records if r.get("hard_verdict") in ("pass", "fail")]
    strata = list(dict.fromkeys(r["category"] for r in records if r.get("category")))
    by_arm = {
        arm: summarize_arm([r for r in records if r.get("arm") == arm]) for arm in ARMS
    }
    by_stratum = {
        stratum: {
            arm: summarize_arm(
                [r for r in records if r.get("category") == stratum and r.get("arm") == arm]
            )
            for arm in ARMS
        }
        for stratum in strata
    }
    return {
        "counts": {
            "records": len(records),
            "hard_pass": sum(1 for r in decided if r["hard_verdict"] == "pass"),
            "hard_fail": sum(1 for r in decided if r["hard_verdict"] == "fail"),
            "judged": sum(1 for r in records if r.get("judged")),
            "errors": sum(1 for r in records if r.get("status") == "error"),
            "pending": sum(1 for r in records if r.get("status") not in ("ok", "error")),
        },
        "by_arm": by_arm,
        "by_stratum": by_stratum,
        "paired": paired_delta(records),
    }


# ---------------------------------------------------------------------------
# artifacts
# ---------------------------------------------------------------------------


def write_records(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_payload(
    *,
    answers_path: Path,
    dataset_path: Path,
    records: Sequence[Mapping[str, Any]],
    data: Mapping[str, Any],
    model: str,
    timestamp: str,
    wall_s: float,
) -> dict:
    payload = {
        "timestamp": timestamp,
        "answers": str(answers_path),
        "answers_sha256": file_digest(answers_path / "records.jsonl"),
        "dataset": str(dataset_path),
        "dataset_sha256": file_digest(dataset_path),
        "judge": {
            "model": model,
            "temperature": 0.0,
            "max_tokens": JUDGE_MAX_TOKENS,
            "system": SYSTEM_PROMPT,
            "schema": JudgeScores.model_json_schema(),
            "evidence_char_cap": EVIDENCE_CHAR_CAP,
        },
        "wall_time_s": round(wall_s, 1),
        "judge_call_time_s": round(
            sum(float(r.get("elapsed_s") or 0) for r in records), 1
        ),
        "hard_failures": [
            {"id": r["id"], "arm": r["arm"], "reasons": r.get("hard_reasons")}
            for r in records
            if r.get("hard_verdict") == "fail"
        ],
        "errors": [
            {"id": r["id"], "arm": r["arm"], "error": r.get("error")}
            for r in records
            if r.get("status") == "error"
        ],
        **data,
    }
    return payload


def _fmt(value: Any) -> str:
    return "-" if value is None else f"{value:.2f}"


def _delta(value: Any) -> str:
    return "-" if value is None else f"{value:+.2f}"


def print_summary(payload: Mapping[str, Any]) -> None:
    counts = payload["counts"]
    print(
        f"records={counts['records']} hard_pass={counts['hard_pass']} "
        f"hard_fail={counts['hard_fail']} judged={counts['judged']} "
        f"errors={counts['errors']} pending={counts['pending']}"
    )
    for arm in ARMS:
        s = payload["by_arm"][arm]
        scores = s["scores"]
        print(
            f"{arm:5} hard {s['hard_pass']}/{s['hard_pass'] + s['hard_fail']} "
            f"({s['hard_pass_rate']:.0%}) "
            f"corr {_fmt(scores['correctness']['mean'])} "
            f"cond {_fmt(scores['condition_preservation']['mean'])} "
            f"cite {_fmt(scores['citation_support']['mean'])} "
            f"unsupported {s['unsupported_claims']['total']} "
            f"cite2+claims {s['claims_with_full_citation_support']} "
            f"coverage {_fmt(s['coverage_mean'])}"
        )
    for stratum, arms in payload["by_stratum"].items():
        cells = []
        for arm in ARMS:
            s = arms[arm]
            cells.append(
                f"{arm} corr {_fmt(s['scores']['correctness']['mean'])} "
                f"cond {_fmt(s['scores']['condition_preservation']['mean'])} "
                f"cite {_fmt(s['scores']['citation_support']['mean'])} "
                f"unsupported {s['unsupported_claims']['total']}"
            )
        print(f"{stratum:18} " + " | ".join(cells))
    paired = payload["paired"]
    print(
        f"graph-text on {paired['n']} paired judged: "
        f"corr {_delta(paired['correctness_mean_delta'])} "
        f"cond {_delta(paired['condition_preservation_mean_delta'])} "
        f"cite {_delta(paired['citation_support_mean_delta'])} "
        f"(better {paired['better']}, worse {paired['worse']}, "
        f"equal {paired['equal']})"
    )
    if payload["hard_failures"]:
        print(f"hard failures: {len(payload['hard_failures'])}")
    if payload["errors"]:
        print(f"errors: {len(payload['errors'])} (resumed on the next --resume)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_out() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return JUDGEMENTS_DIR / stamp


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--answers", type=Path, default=DEFAULT_ANSWERS)
    ap.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    ap.add_argument("--out", type=Path, default=None, help="artifact directory (UTC timestamp by default)")
    ap.add_argument("--limit", type=int, default=0, help="judge at most N pending records, in batch order")
    ap.add_argument("--sample", type=int, default=0, help="judge N pending records spread over the batch")
    ap.add_argument("--resume", action="store_true", help="reuse the --out checkpoint and judge what is missing")
    args = ap.parse_args()

    if args.limit and args.sample:
        ap.error("--limit and --sample are mutually exclusive")
    if args.resume and args.out is None:
        ap.error("--resume needs --out pointing at the interrupted run's directory")

    from paknow.config import settings  # noqa: E402
    from paknow.gateway import gateway  # noqa: E402

    out_dir = args.out or _default_out()
    out_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = out_dir / "checkpoint.jsonl"

    records = load_records(args.answers / "records.jsonl")
    items = load_items(args.dataset)
    order = [(str(r["id"]), str(r["arm"])) for r in records]
    by_key = {(str(r["id"]), str(r["arm"])): r for r in records}
    checkpoint = load_checkpoint(checkpoint_path) if args.resume else {}
    todo = pending_keys(order, checkpoint)
    if args.sample:
        todo = spread(todo, args.sample)
    elif args.limit:
        todo = todo[: args.limit]

    wanted: set[str] = set()
    for key in order:
        record = by_key[key]
        wanted.update(str(e) for e in record.get("cited_evidence_ids") or [])
        wanted.update(str(e) for e in record.get("expected_evidence") or [])
    evidence_texts = fetch_evidence_texts(wanted)

    def ask(schema: type[BaseModel], messages: list[dict[str, str]]) -> BaseModel:
        # the reasoning budget shares max_tokens; a long answer plus a 0-2 rubric
        # needs the headroom or the content comes back empty at finish=length
        return gateway.chat_json(schema, messages, max_tokens=JUDGE_MAX_TOKENS, temperature=0.0)

    started = time.monotonic()
    for index, key in enumerate(todo, 1):
        record = by_key[key]
        verdict = judge_record(record, items[key[0]], evidence_texts, ask)
        append_checkpoint(checkpoint_path, verdict)
        checkpoint[key] = verdict
        if verdict["judged"]:
            detail = (
                f"corr={verdict['correctness']} cond={verdict['condition_preservation']} "
                f"cite={verdict['citation_support']} "
                f"unsupported={len(verdict['unsupported_claims'])}"
            )
        elif verdict["status"] == "error":
            detail = f"ERROR {verdict['error']}"
        else:
            detail = f"HARD FAIL {','.join(verdict['hard_reasons'])}"
        print(
            f"[{index}/{len(todo)}] {key[0]} {key[1]} {detail} "
            f"{verdict['elapsed_s']:.1f}s",
            flush=True,
        )
    wall = time.monotonic() - started

    judged_records = merge_judged(order, checkpoint)
    for_aggregate = [
        checkpoint.get(key) or {"id": key[0], "arm": key[1]} for key in order
    ]
    data = aggregate(for_aggregate)
    timestamp = out_dir.name
    summary_path = out_dir / "summary.json"
    previous_wall = 0.0
    if summary_path.exists():
        try:
            previous_wall = float(
                json.loads(summary_path.read_text(encoding="utf-8")).get("wall_time_s") or 0
            )
        except (ValueError, json.JSONDecodeError):
            previous_wall = 0.0
    payload = build_payload(
        answers_path=args.answers,
        dataset_path=args.dataset,
        records=judged_records,
        data=data,
        model=settings.deepseek_model,
        timestamp=timestamp,
        wall_s=previous_wall + wall,
    )
    payload["ran"] = len(todo)
    payload["evidence_units_fetched"] = len(evidence_texts)
    write_records(out_dir / "records.jsonl", judged_records)
    summary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print_summary(payload)
    print(f"\nwrote {out_dir} (judged {len(todo)} of {len(order)} records)")


if __name__ == "__main__":
    main()
