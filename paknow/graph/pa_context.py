"""Causal PA tags drive a small verified concept → question → passage graph."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import median
from typing import Any, Literal

from sqlalchemy import select

from paknow.db import SessionLocal
from paknow.db.models import ClaimRow, GraphEdgeRow
from paknow.graph.identity import canonical_for, concept_node_id, label_for
from paknow.pa_background import LOOKBACK
from paknow.pa_rules import STEP, Bar

Role = Literal["support", "risk", "confirmation"]
ROLES: tuple[Role, ...] = ("support", "risk", "confirmation")
SCOPE = "pa_text_graph"
ROLE_RELATIONS = {"support": "REQUIRES_CONTEXT", "risk": "EXCEPTION_TO", "confirmation": "REQUIRES_CONTEXT"}
RISK_PROFILE_FAMILY = {"edge": "range_breakout", "mixed": "range_breakout", "reentry": "range_breakout"}
ROLE_PROFILES = {
    "support": frozenset({"trend", "range_fade", "repeated", "mixed", "counter"}),
    "risk": frozenset({"proxy", "mixed", "any", "edge", "stretched", "counter", "trend", "reentry", "obstacle"}),
    "confirmation": frozenset({"any", "edge", "signal_only"}),
}


def valid_relation(role: str, profile: str, relation: str) -> bool:
    return relation == ROLE_RELATIONS.get(role) or (role, profile, relation) == ("risk", "reentry", "INVALIDATED_BY")


@dataclass(frozen=True, slots=True)
class Setup:
    side: str
    regime: str
    alignment: str
    zone: str
    test: str
    signal: str
    edge: float
    edge_ms: int
    obstacle_before_target: bool
    reentered: bool
    large_signal: bool
    observed_test_ms: tuple[int, ...]
    repeated_with_reaction: bool

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def setup_at(state: dict[str, Any], bars: tuple[Bar, ...]) -> Setup:
    """No next bar, future follow-through, plan fills, or historical verdicts."""
    if (len(bars) != LOOKBACK + 1 or any(a.ts_open + STEP != b.ts_open for a, b in zip(bars, bars[1:]))
            or bars[-1].ts_open + STEP != state["close_ms"]):
        raise ValueError("expected exactly 80 contiguous bars and the selected closed signal")
    side = state["original_state"]
    if side not in {"long", "short"}:
        raise ValueError("only a nominated candidate has a review setup")
    sign = 1 if side == "long" else -1
    signal, prior = bars[-1], bars[:-1]
    past = prior[-1]
    top, bottom = max(x.high for x in prior), min(x.low for x in prior)
    bound = max(prior, key=lambda x: (x.high, x.ts_open)) if sign > 0 else min(prior, key=lambda x: (x.low, -x.ts_open))
    edge = bound.high if sign > 0 else bound.low
    pos = (signal.close - bottom) / (top - bottom) if top > bottom else 0.5
    zone = "above" if pos > 1 else "below" if pos < 0 else "upper" if pos >= 0.8 else "lower" if pos <= 0.2 else "middle"
    trigger = next((x["passed"] for x in state[side]["checks"] if x["id"] == "trigger"), False)
    past_edge = past.high if sign > 0 else past.low
    confirm = "prior_extreme_close" if trigger and sign * (signal.close - past_edge) > 0 else "same_side_close" if trigger else "not_confirmed"
    tests = tuple(bars[-i].ts_open for i in state[side]["test_bars"] if 2 <= i <= LOOKBACK + 1)
    test = "observed_by_rule" if tests else "ema_proxy" if state[side]["ema_proxy"] else "not_observed"
    typical = median(x.high - x.low for x in prior[-20:])
    # A stricter descriptive check: two distinct probes around the earlier level
    # with an intervening move away. This does not relabel the original check.
    probe = past.low if sign > 0 else past.high
    repeated = False
    for i in range(3, LOOKBACK + 2):
        first = bars[-i]
        if first.ts_open not in tests or i < 4 or typical <= 0:
            continue
        level = first.low if sign > 0 else first.high
        reaction = max(sign * (b.close - level) for b in bars[-i + 1:-2])
        repeated |= abs(probe - level) <= 0.6 * typical and reaction >= typical
    # A close exactly on the boundary is a warning, not a close back inside.
    reentered = (sign * ((signal.high if sign > 0 else signal.low) - edge) > 0
                 and bottom < signal.close < top)
    return Setup(side, state["background"]["regime"], state["plan"]["alignment"], zone, test, confirm, edge,
                 bound.ts_open, state["plan"]["edge_before_target"] is not None,
                 reentered,
                 typical > 0 and signal.high - signal.low >= 1.8 * typical, tests, repeated)


def base_questions(setup: Setup) -> dict[Role, str]:
    """Deterministic fallback; tags describe questions, never admission rules."""
    side = "多头高二" if setup.side == "long" else "空头低二"
    shape = {"ema_proxy": "只有均线接近，没有规则二次测试", "observed_by_rule": "同方向再次测试附近极值",
             "not_observed": "尚未看见第二次测试"}[setup.test]
    if setup.repeated_with_reaction:
        shape = "有两个不同的探测点，且中间曾出现反应"
    backdrop = "震荡区间" if setup.alignment == "mixed" else "逆前段走势" if setup.alignment == "counter" else "顺势回调"
    forward_edge = setup.zone in ({"upper", "above"} if setup.side == "long" else {"lower", "below"})
    hazard = ("已越过前 80 根同向边界并收回区间内" if setup.reentered
              else "靠近或收在同向边界外时，需要防哪些后续假突破" if setup.alignment == "mixed" and forward_edge
              else "逆势反转没有跟进" if setup.alignment == "counter" else "趋势衰竭或回调过深")
    obstacle = "目标之前还有同向区间边缘障碍。" if setup.obstacle_before_target else ""
    return {
        "support": f"{backdrop}中的{side}在什么条件下适用？{shape}说明什么？",
        "risk": f"{backdrop}的{side}可能为什么失败？{hazard}。{obstacle}{shape}。",
        "confirmation": f"{side}怎样区分信号收盘、突破或回调入场与后续同向跟进的确认？",
    }


@dataclass(frozen=True, slots=True)
class Lead:
    role: Role
    concept: str
    question: str
    evidence_id: str
    sha256: str
    relation: str
    profile: str
    condition: str = ""

    def reason(self) -> dict[str, str]:
        return {"concept": label_for(self.concept), "concept_id": self.concept, "relation": self.relation,
                "profile": self.profile, "question": self.question, "condition": self.condition}


def _profiles(s: Setup) -> tuple[str, ...]:
    profiles = []
    near_edge = s.zone in ({"upper", "above"} if s.side == "long" else {"lower", "below"})
    opposite_edge = s.zone == ("lower" if s.side == "long" else "upper")
    if s.alignment == "mixed":
        if s.reentered:
            profiles.append("reentry")
        if near_edge or s.reentered:
            profiles.append("edge")
        if opposite_edge:
            profiles.append("range_fade")
    if s.test == "ema_proxy":
        profiles.append("proxy")
    if s.alignment == "mixed" and s.obstacle_before_target:
        profiles.append("obstacle")
    if s.signal == "same_side_close":
        profiles.append("signal_only")
    if s.alignment == "counter":
        profiles.append("counter")
    if s.alignment == "aligned" and s.large_signal:
        profiles.append("stretched")
    if s.repeated_with_reaction:
        profiles.append("repeated")
    profiles.extend(("mixed" if s.alignment == "mixed" else "trend" if s.alignment == "aligned" else "counter", "any"))
    return tuple(dict.fromkeys(profiles))


def plan(setup: Setup) -> tuple[Lead, ...]:
    """Follow only reviewed PA edges. General heading links stay navigation-only."""
    terms = ["second_entry", "breakout", "failed_breakout", "high_2" if setup.side == "long" else "low_2"]
    if setup.alignment != "mixed":
        terms += ["pullback", "trend", "breakout_pullback"]
    symbols = {canonical for term in terms if (canonical := canonical_for(term))}
    profiles = _profiles(setup)
    with SessionLocal() as db:
        claims = db.scalars(select(ClaimRow).where(ClaimRow.source_scope == SCOPE,
                            ClaimRow.extraction_status == "reviewed", ClaimRow.subject_concept_id.in_(symbols))).all()
        keys = [r.claim_id for r in claims]
        related = db.scalars(select(GraphEdgeRow).where(GraphEdgeRow.origin == SCOPE,
                             GraphEdgeRow.extraction_status == "reviewed",
                             GraphEdgeRow.from_node.in_(keys) | GraphEdgeRow.to_node.in_(keys))).all() if keys else []
        edges = {(r.from_node, r.to_node, r.relation, r.source) for r in related}
    leads: list[tuple[Lead, bool]] = []
    for claim in claims:
        meta = claim.qualifiers
        role, profile = meta.get("role"), meta.get("profile")
        if role not in ROLES or profile not in profiles or meta.get("side") not in (setup.side, "any"):
            continue
        if len(claim.supporting_evidence) != 1 or not meta.get("sha256"):
            continue
        concept, (eid,) = claim.subject_concept_id, claim.supporting_evidence
        relation = meta.get("relation", ROLE_RELATIONS[role])
        if profile not in ROLE_PROFILES[role] or not valid_relation(role, profile, relation):
            continue
        if ((concept_node_id(concept), claim.claim_id, relation, eid) not in edges
                or (claim.claim_id, eid, "SUPPORTED_BY", eid) not in edges):
            continue
        leads.append((Lead(role, concept, claim.predicate, eid, meta["sha256"], relation, profile,
                           meta.get("condition", "")), meta["side"] == "any"))

    def priority(item: tuple[Lead, bool]) -> tuple:
        lead, generic_side = item
        background_support = lead.role == "support" and lead.profile != "repeated"
        return not background_support, profiles.index(lead.profile), generic_side, lead.evidence_id

    ordered = [lead for lead, _ in sorted(leads, key=priority)]
    selected = []
    for role in ROLES:
        matching = [lead for lead in ordered if lead.role == role]
        if not matching:
            continue
        if role == "support":
            selected.append(matching[0])
            # Keep the background lead and make the actual repeated structure
            # queryable, rather than spending both slots on nested backgrounds.
            repeated = next((lead for lead in matching[1:] if lead.profile == "repeated"), None)
            if repeated or len(matching) > 1:
                selected.append(repeated or matching[1])
            continue
        if role == "confirmation":
            selected.extend(matching[:2])
            continue
        # Keep one primary question per risk family. The range warning cannot
        # crowd out a structurally different proxy or intervening obstacle.
        primary: dict[str, Lead] = {}
        for lead in matching:
            primary.setdefault(RISK_PROFILE_FAMILY.get(lead.profile, lead.profile), lead)
        selected.extend(list(primary.values())[:3])
    return tuple(selected)
