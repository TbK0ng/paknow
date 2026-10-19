"""Bar-reference lookup: map "K线N" mentions in queries to structured figure
annotations (plan §6.1 BarRef)."""
from __future__ import annotations

import re
from typing import Any

from paknow.db import SessionLocal
from paknow.db.models import BarRefRow, FigureAssetRow

_BAR_RE = re.compile(r"(?:K\s*线|bar|bars?)\s*(\d+)(?:\s*[-–到至]\s*(\d+))?", re.I)
_FIG_RE = re.compile(r"(?:图|figure)\s*(\d+(?:\.\d+)?)", re.I)


def _query_bar_numbers(query: str) -> set[str]:
    nums: set[str] = set()
    for m in _BAR_RE.finditer(query):
        a, b = m.group(1), m.group(2)
        if b:
            for n in range(int(a), int(b) + 1):
                nums.add(str(n))
        else:
            nums.add(a)
    return nums


def _ref_bar_numbers(bars: list[str]) -> set[str]:
    nums: set[str] = set()
    for raw in bars:
        m = re.search(r"(\d+)(?:\s*[-–到至]\s*(\d+))?", raw or "")
        if not m:
            continue
        a, b = m.group(1), m.group(2)
        if b:
            for n in range(int(a), int(b) + 1):
                nums.add(str(n))
        else:
            nums.add(a)
    return nums


def barref_lookup(query: str, top_k: int = 10) -> list[dict[str, Any]]:
    """Return bar-refs whose bar numbers (and/or figure label) match the query."""
    qbars = _query_bar_numbers(query)
    fig_labels = {m.group(1) for m in _FIG_RE.finditer(query)}
    if not qbars and not fig_labels:
        return []
    with SessionLocal() as s:
        fig_ids: set[str] = set()
        if fig_labels:
            fig_ids = {
                f.figure_id
                for f in s.query(FigureAssetRow)
                .filter(FigureAssetRow.figure_label.in_(fig_labels))
                .all()
            }
        rows = s.query(BarRefRow).all()
    scored: list[tuple[int, dict[str, Any]]] = []
    for r in rows:
        rb = _ref_bar_numbers(r.bars or [])
        inter = qbars & rb
        in_fig = bool(fig_ids and r.figure_id in fig_ids)
        if not inter and not in_fig:
            continue
        score = len(inter) * 2 + (1 if in_fig else 0)
        scored.append(
            (
                score,
                {
                    "ref_id": r.ref_id,
                    "figure_id": r.figure_id,
                    "evidence_id": r.evidence_id,
                    "pattern": r.pattern or "",
                    "bars": r.bars or [],
                    "relation": r.relation or "",
                    "snippet": r.snippet or "",
                },
            )
        )
    scored.sort(key=lambda x: -x[0])
    return [d for _s, d in scored[:top_k]]
