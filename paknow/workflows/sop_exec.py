"""Interpret a selected SOP into retrieval-state directives (plan §9.3).

The default query flow already implements the standard ops (step-back /
multi-query / hyde / hybrid / rerank); the SOP adds intent-specific
parameterization: extra queries, evidence requirements, fallback and stop
rules. Unknown ops are recorded (visible in the run trace) rather than
silently ignored.
"""
from __future__ import annotations

from typing import Any

from paknow.retrieval import dictionary


def apply_sop(sop: dict[str, Any], state: dict[str, Any], query: str) -> None:
    state["sop_id"] = sop["sop_id"]
    state["sop_status"] = sop["status"]
    extra_queries: list[str] = []
    for step in sop.get("steps") or []:
        op = str(step.get("operation") or "").strip().lower()
        if op == "expand_terms":
            terms = dictionary.expand_query(query)
            extra_queries.extend(terms["canonical"][:3])
        elif op == "step_back":
            if not state.get("step_back"):
                state["step_back"] = query
        elif op == "multi_query":
            state["sop_multi_query"] = True
        elif op == "hyde":
            state["use_hyde"] = True
        elif op == "expand_figures":
            state["sop_expand_figures"] = True
        elif op == "check_evidence":
            state["sop_evidence_requirements"] = list(sop.get("required_evidence") or [])
        elif op == "rerank":
            state["sop_rerank"] = True
        elif op in ("hybrid_search",):
            pass  # default retrieval is already hybrid
        else:
            state.setdefault("sop_skipped_ops", []).append(op)
    if extra_queries:
        state["sop_extra_queries"] = extra_queries
    if sop.get("fallback"):
        state["sop_fallback"] = sop["fallback"]
    if sop.get("stop"):
        state["sop_stop"] = sop["stop"]
