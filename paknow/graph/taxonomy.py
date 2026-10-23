"""Concept taxonomy built deterministically from encyclopedia heading grammar.

The encyclopedia names entries as ``情境：形态 + 结果``. The colon splits the
situation from the entry/outcome, and a small closed vocabulary of markers maps
to existing :class:`Relation` members:

- ``失败``            -> ``INVALIDATED_BY``
- ``随后``            -> ``TRANSITIONS_TO``
- ``例外`` / ``但…恢复|继续`` -> ``EXCEPTION_TO``
- explicit parent cue (``变体`` / ``一种`` / ``属于``, or an ``…中的…`` context)
  -> ``PART_OF``

Anything that does not yield two distinct dictionary concepts is dropped; the
parser never falls back to ``PART_OF``. Pure functions (:func:`dedupe_headings`,
:func:`parse_heading`, :func:`build_taxonomy_edges`) take a batch of headings and
return edges, so they are testable without a database or a model.

Rebuilds replace only ``origin='heading_parse'`` edges; manually seeded edges
(``origin='seed'``) survive, and the result is idempotent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from sqlalchemy import or_

from paknow.db import SessionLocal
from paknow.db.models import EvidenceUnitRow, GraphEdgeRow, GraphNodeRow
from paknow.graph.contracts import EdgeOrigin, Relation
from paknow.graph.identity import (
    canonical_for,
    concept_kind_for,
    concept_node_id,
    label_for,
    resolved_canonical,
)
from paknow.ids import stable_id
from paknow.retrieval.dictionary import TermMatch, find_terms

TAXONOMY_RELATIONS: tuple[Relation, ...] = (
    Relation.PART_OF,
    Relation.EXCEPTION_TO,
    Relation.INVALIDATED_BY,
    Relation.TRANSITIONS_TO,
)

_HEADER_THRESHOLD = 5
_WS = re.compile(r"\s+")
_CONNECTORS = re.compile(r"[\s，,。；;：:、/\\（）()\[\]【】“”\"'·\-—]+")
_CONTEXT_WORDS = re.compile(r"中的|之中的|的|中|里|下方|上方|之后|之前|或|和|与|及|属于|一种|变体|是|为")
# table-of-contents titles (the opening ~10-page table); TOC *rows* never load
# because they are paragraph blocks, not headings
_TOC_TITLE = re.compile(
    r"^(?:(?:未?分类)?目录|(?:table of )?contents)(?:\s.*)?$", re.IGNORECASE
)


@dataclass(frozen=True)
class HeadingUnit:
    evidence_id: str
    doc_id: str
    text: str


@dataclass(frozen=True)
class ParsedHeading:
    relation: Relation
    from_concept: str
    to_concept: str


@dataclass(frozen=True)
class TaxonomyEdge:
    from_concept: str
    relation: Relation
    to_concept: str
    source: str
    origin: str = EdgeOrigin.heading_parse.value


@dataclass
class DedupeResult:
    kept: list[HeadingUnit]
    stats: dict[str, int] = field(default_factory=dict)


@dataclass
class TaxonomyBuild:
    edges: list[TaxonomyEdge]
    stats: dict[str, Any] = field(default_factory=dict)


def normalize_heading(text: str) -> str:
    """Collapse whitespace/NBSP so repeated page furniture normalizes equal."""
    return _WS.sub(" ", (text or "").replace("\xa0", " ")).strip()


def is_toc_title(text: str) -> bool:
    """True for the front-matter table-of-contents heading itself.

    The TOC's *rows* are paragraph blocks and never reach the heading load;
    this guard covers the TOC title, which must not be parsed into an edge or
    counted as body coverage. Body headings (with a ``：`` situation/outcome
    split) never match.
    """
    first_line = normalize_heading(text).split("\n", 1)[0].strip()
    return bool(_TOC_TITLE.fullmatch(first_line))


def dedupe_headings(
    units: Iterable[HeadingUnit], *, header_threshold: int = _HEADER_THRESHOLD
) -> DedupeResult:
    """Keep the first occurrence of a normalized title per document.

    A title repeated at least ``header_threshold`` times in one document is
    page furniture (running heads), so every occurrence is dropped.
    """
    ordered = list(units)
    counts: dict[tuple[str, str], int] = {}
    for u in ordered:
        key = (u.doc_id, normalize_heading(u.text))
        counts[key] = counts.get(key, 0) + 1

    kept: list[HeadingUnit] = []
    seen: set[tuple[str, str]] = set()
    dropped_header = dropped_duplicate = 0
    for u in ordered:
        key = (u.doc_id, normalize_heading(u.text))
        if not key[1]:
            continue
        if counts[key] >= header_threshold:
            dropped_header += 1
            continue
        if key in seen:
            dropped_duplicate += 1
            continue
        seen.add(key)
        kept.append(HeadingUnit(u.evidence_id, u.doc_id, key[1]))

    return DedupeResult(
        kept=kept,
        stats={
            "headings_in": len(ordered),
            "headings_after_dedupe": len(kept),
            "dropped_header": dropped_header,
            "dropped_duplicate": dropped_duplicate,
        },
    )


def _match_concept(match: TermMatch) -> str:
    """Resolve an alias to the dictionary's primary canonical concept.

    Chapter-derived ``auto_*`` dictionary entries repeat the aliases of the base
    vocabulary (``auto_trading_range_tr`` also claims ``交易区间``). Identity
    resolution keeps the first registered canonical for a surface form, so this
    pins a title mentioning ``交易区间`` on ``trading_range`` instead of minting a
    second concept node with the same label.
    """
    return resolved_canonical(match.alias, match.canonical)


def _failed_variant(canonical: str) -> str:
    """``突破`` + ``失败`` resolves to the dictionary's ``failed_breakout``."""
    if canonical.startswith("failed_"):
        return canonical
    candidate = f"failed_{canonical}"
    return canonical_for(candidate) or canonical


def _only_concepts(text: str, matches: Sequence[TermMatch]) -> bool:
    """True when nothing but concept mentions/connectors remains."""
    if not matches:
        return False
    chars = list(text)
    for m in matches:
        for i in range(m.start, m.end):
            chars[i] = ""
    rest = _CONNECTORS.sub("", "".join(chars))
    rest = _CONTEXT_WORDS.sub("", rest)
    return rest.strip() == ""


def parse_heading(text: str) -> ParsedHeading | None:
    """Parse one heading into a directed concept edge, or drop it."""
    normalized = normalize_heading(text)
    left, sep, right = normalized.partition("：")
    if not sep:
        return None
    left, right = left.strip(), right.strip()
    if not left or not right:
        return None
    left_matches = find_terms(left)
    right_matches = find_terms(right)
    if not left_matches or not right_matches:
        return None
    left_concepts = [_match_concept(m) for m in left_matches]
    right_concepts = [_match_concept(m) for m in right_matches]

    # the head of the situation is the concept right before a 失败 marker (the
    # failed variant), otherwise its first concept; the outcome's head is last
    failure_at = left.find("失败")
    if failure_at >= 0:
        before = [_match_concept(m) for m in left_matches if m.end <= failure_at]
        from_concept = _failed_variant(before[-1]) if before else left_concepts[0]
    else:
        from_concept = left_concepts[0]
    to_concept = right_concepts[-1]

    if "随后" in right:
        relation = Relation.TRANSITIONS_TO
    elif "例外" in right or ("但" in right and ("恢复" in right or "继续" in right)):
        relation = Relation.EXCEPTION_TO
    elif "失败" in right:
        relation = Relation.INVALIDATED_BY
    elif any(cue in right for cue in ("变体", "一种", "属于")):
        relation = Relation.PART_OF
    elif "失败" in left:
        relation = Relation.INVALIDATED_BY
    elif re.search(r"中的|之中的", left) and _only_concepts(left, left_matches) and _only_concepts(right, right_matches):
        # "context：entry" where the entry is a subtype of the context concept
        relation = Relation.PART_OF
        from_concept, to_concept = right_concepts[-1], left_concepts[-1]
    else:
        return None

    if from_concept == to_concept:
        return None
    return ParsedHeading(relation=relation, from_concept=from_concept, to_concept=to_concept)


def build_taxonomy_edges(
    units: Iterable[HeadingUnit], *, header_threshold: int = _HEADER_THRESHOLD
) -> TaxonomyBuild:
    """Dedupe, exclude TOC titles, parse, and deduplicate edges.

    Same input -> same edge set. ``excluded_toc`` counts front-matter TOC
    headings that never participated in parsing; it is the coverage
    cross-check for the spec's "TOC is not extracted" rule.
    """
    ordered = list(units)
    body = [u for u in ordered if not is_toc_title(u.text)]
    excluded_toc = len(ordered) - len(body)
    deduped = dedupe_headings(body, header_threshold=header_threshold)
    # key -> lexicographically smallest locator, so input order cannot matter
    by_key: dict[tuple[str, str, str], str] = {}
    parsed = 0
    for unit in deduped.kept:
        result = parse_heading(unit.text)
        if result is None:
            continue
        parsed += 1
        key = (result.from_concept, result.relation.value, result.to_concept)
        current = by_key.get(key)
        if current is None or unit.evidence_id < current:
            by_key[key] = unit.evidence_id

    edges = [
        TaxonomyEdge(from_concept=frm, relation=Relation(rel), to_concept=to, source=src)
        for (frm, rel, to), src in sorted(by_key.items())
    ]
    after = len(deduped.kept)
    stats: dict[str, Any] = {
        **deduped.stats,
        "headings_loaded": len(ordered),
        "excluded_toc": excluded_toc,
        "parsed": parsed,
        "dropped_unparsed": after - parsed,
        "edges": len(edges),
        "coverage": round(parsed / after, 4) if after else 0.0,
    }
    return TaxonomyBuild(edges=edges, stats=stats)


def _concepts_in(edges: Iterable[TaxonomyEdge]) -> set[str]:
    return {c for e in edges for c in (e.from_concept, e.to_concept)}


def load_heading_units(
    *, doc_ids: Iterable[str] | None = None, session_factory=None
) -> list[HeadingUnit]:
    """Heading evidence units in deterministic order, optionally scoped to docs.

    Only ``block_type == "heading"`` loads, so table-of-contents *rows* (stored
    as paragraph blocks) can never enter parsing; :func:`build_taxonomy_edges`
    then drops the TOC title itself and reports the count.
    """
    factory = session_factory or SessionLocal
    with factory() as s:
        query = s.query(EvidenceUnitRow).filter(EvidenceUnitRow.block_type == "heading")
        if doc_ids is not None:
            ids = sorted(doc_ids)
            if not ids:
                return []
            query = query.filter(EvidenceUnitRow.doc_id.in_(ids))
        rows = query.order_by(EvidenceUnitRow.doc_id, EvidenceUnitRow.evidence_id).all()
        return [HeadingUnit(r.evidence_id, r.doc_id, r.text) for r in rows]


def rebuild_taxonomy(
    *,
    header_threshold: int = _HEADER_THRESHOLD,
    doc_ids: Iterable[str] | None = None,
    session_factory=None,
) -> dict[str, Any]:
    """Rebuild heading-derived taxonomy edges; other origins are untouched.

    ``doc_ids`` scopes the parse (the encyclopedia, in the real corpus); ``None``
    reads every heading in the database.
    """
    factory = session_factory or SessionLocal
    build = build_taxonomy_edges(
        load_heading_units(doc_ids=doc_ids, session_factory=factory),
        header_threshold=header_threshold,
    )

    with factory() as s:
        existing_nodes = {
            n.node_id: n
            for n in s.query(GraphNodeRow).filter(GraphNodeRow.node_type == "concept").all()
        }
        existing_concepts = {n.concept_id for n in existing_nodes.values() if n.concept_id}
        for canonical in sorted(_concepts_in(build.edges) - existing_concepts):
            node_id = concept_node_id(canonical)
            node = existing_nodes.get(node_id)
            if node is not None:
                # a pre-existing node without an identity inherits it instead of
                # producing a duplicate row with the same primary key
                node.concept_id = canonical
                node.label = node.label or label_for(canonical)
                node.concept_kind = node.concept_kind or concept_kind_for(canonical)
                continue
            s.add(
                GraphNodeRow(
                    node_id=node_id,
                    node_type="concept",
                    label=label_for(canonical),
                    concept_id=canonical,
                    concept_kind=concept_kind_for(canonical),
                )
            )
        # origin names the producer, so a full rebuild replaces every heading
        # edge even if the parser's relation set changed
        (
            s.query(GraphEdgeRow)
            .filter(GraphEdgeRow.origin == EdgeOrigin.heading_parse.value)
            .delete(synchronize_session=False)
        )
        for e in build.edges:
            from_node = concept_node_id(e.from_concept)
            to_node = concept_node_id(e.to_concept)
            s.add(
                GraphEdgeRow(
                    edge_id=stable_id("edge", from_node, to_node, e.relation.value, e.origin),
                    from_node=from_node,
                    to_node=to_node,
                    relation=e.relation.value,
                    source=e.source,
                    origin=e.origin,
                    extraction_status="pending",
                )
            )
        s.commit()
    return build.stats


def taxonomy_neighbors(
    concept_ids: Iterable[str],
    *,
    relations: Iterable[Relation | str] | None = None,
    limit: int = 50,
    session_factory=None,
) -> list[dict[str, Any]]:
    """List taxonomy edges touching ``concept_ids`` (canonical concept ids).

    ``direction`` is relative to the queried concepts: ``out`` when the edge
    starts at one of them. ``locator`` points back to the heading evidence that
    proved the edge, so a reader can verify the relationship.
    """
    ids = [c for c in concept_ids if c]
    if not ids:
        return []
    allowed = tuple(r.value if isinstance(r, Relation) else str(r) for r in (relations or TAXONOMY_RELATIONS))
    node_ids = {concept_node_id(c) for c in ids}
    factory = session_factory or SessionLocal

    with factory() as s:
        rows = (
            s.query(GraphEdgeRow)
            .filter(
                GraphEdgeRow.relation.in_(allowed),
                or_(GraphEdgeRow.from_node.in_(node_ids), GraphEdgeRow.to_node.in_(node_ids)),
            )
            .order_by(
                GraphEdgeRow.relation,
                GraphEdgeRow.from_node,
                GraphEdgeRow.to_node,
                GraphEdgeRow.origin,
                GraphEdgeRow.source,
                GraphEdgeRow.edge_id,
            )
            .limit(limit)
            .all()
        )
        node_rows = (
            s.query(GraphNodeRow)
            .filter(GraphNodeRow.node_id.in_({e.from_node for e in rows} | {e.to_node for e in rows}))
            .all()
        )
        by_node = {n.node_id: n for n in node_rows}
        sources = {e.source for e in rows}
        evidence = {
            u.evidence_id: u
            for u in s.query(EvidenceUnitRow).filter(EvidenceUnitRow.evidence_id.in_(sources)).all()
        }

    def endpoint(node_id: str) -> dict[str, Any]:
        node = by_node.get(node_id)
        if node is None:
            return {"concept_id": node_id, "label": node_id}
        return {"concept_id": node.concept_id or node.node_id, "label": node.label}

    out: list[dict[str, Any]] = []
    for e in rows:
        unit = evidence.get(e.source)
        out.append(
            {
                "from": endpoint(e.from_node),
                "relation": e.relation,
                "to": endpoint(e.to_node),
                "direction": "out" if e.from_node in node_ids else "in",
                "origin": e.origin,
                "evidence_id": unit.evidence_id if unit else None,
                "locator": {
                    "chapter_path": (unit.chapter_path or []) if unit else [],
                    "pdf_page": unit.pdf_page_1based if unit else None,
                    "printed_page": (unit.locator or {}).get("printed_page_label") if unit else None,
                },
            }
        )
    return out
