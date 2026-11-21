"""Build citable passages, retaining verbatim source blocks and parent context."""
from __future__ import annotations

import re
from collections.abc import Iterator

from paknow.evidence.contracts import DocFormat, EvidenceUnit
from paknow.ids import content_hash, stable_id
from paknow.ingest.figure_labels import FIGURE_ONLY

VERSION = "passages-v2"
BUILD_VERSION = f"pdf-epub-text-v2+{VERSION}"
DEFAULT_MAX_CHARS = 1200
_BOUNDARY = re.compile(r"(?<=[。！？；!?;])\s*|(?<=[.])\s+(?=[A-Z“\"（(])")
_QUALIFIER = re.compile(
    r"^\s*(?:[•▪*–-]\s*)?(?:(?:however|but|unless|except|only if|provided that|otherwise|"
    r"in (?:a|the) trading range)\b|但是|不过|然而|除非|仅当|前提是|例外|相反|如果|若在|但[在这若是])", re.I
)


def sentence_spans(text: str) -> Iterator[tuple[int, int]]:
    """Contiguous Python-character offsets, with limiting clauses attached."""
    start = 0
    for sep in _BOUNDARY.finditer(text):
        boundary = sep.end()
        if boundary <= start or boundary >= len(text):
            continue
        if _QUALIFIER.match(text[boundary:]) or re.search(r"\b(?:fig|dr|mr|vs|no)\.$", text[max(0, sep.start() - 8):sep.start()], re.I):
            continue
        yield start, boundary
        start = boundary
    if start < len(text):
        yield start, len(text)


def _chunk_spans(text: str, max_chars: int) -> Iterator[tuple[int, int]]:
    first = end = 0
    for start, stop in sentence_spans(text):
        if end > first and stop - first > max_chars:
            yield first, end
            first = start
        if stop - first > max_chars:
            # Keep a long qualified sentence whole where possible; a very long
            # unpunctuated extraction still needs bounded vector input.
            if stop - first <= max_chars * 2:
                end = stop
                continue
            while stop - first > max_chars:
                boundary = max(text.rfind(" ", first + max_chars // 2, first + max_chars),
                               text.rfind("\n", first + max_chars // 2, first + max_chars))
                cut = boundary + 1 if boundary >= 0 else first + max_chars
                yield first, cut
                first = cut
        end = stop
    if end > first:
        yield first, end


def chunk_text(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> list[str]:
    return [text[a:b] for a, b in _chunk_spans(text, max_chars)] or [text]


def _can_join(left: EvidenceUnit, right: EvidenceUnit) -> bool:
    if (left.block_type != "paragraph" or right.block_type != "paragraph"
            or left.doc_id != right.doc_id or left.provenance != right.provenance
            or FIGURE_ONLY.fullmatch(left.text) or FIGURE_ONLY.fullmatch(right.text)):
        return False
    a, b = left.locator, right.locator
    if (a.format != b.format or a.chapter_path != b.chapter_path
            or a.pdf_page_1based != b.pdf_page_1based or a.epub_href != b.epub_href
            or a.figure_label or b.figure_label):
        return False
    if a.format == DocFormat.epub:
        # Existing author paragraph boundaries are usually meaningful.
        return bool(a.epub_href and not b.epub_fragment and _QUALIFIER.match(right.text))
    if not (a.pdf_page_1based and a.bbox and b.bbox):
        return False
    # Do not join columns, labels, or distant blocks on the same physical page.
    if abs(a.bbox[0] - b.bbox[0]) > 28 or not -3 <= b.bbox[1] - a.bbox[3] <= 20:
        return False
    return bool(_QUALIFIER.match(right.text) or (len(left.text) <= 160 and len(right.text) <= 340))


def _passages(units: list[EvidenceUnit], max_chars: int) -> Iterator[list[EvidenceUnit]]:
    group: list[EvidenceUnit] = []
    size = 0
    for unit in units:
        # A limiting clause may make the reading parent longer than a normal
        # hit. Keep it in the same source group, then split into child spans.
        cap = max_chars * 2 if _QUALIFIER.match(unit.text) else max_chars
        if group and (size + len(unit.text) > cap or len(group) >= 6 or not _can_join(group[-1], unit)):
            yield group
            group, size = [], 0
        group.append(unit)
        size += len(unit.text) + 1
    if group:
        yield group


def chunk_units(units: list[EvidenceUnit], max_chars: int = DEFAULT_MAX_CHARS) -> list[EvidenceUnit]:
    out: list[EvidenceUnit] = []
    for group in _passages(units, max_chars):
        root = group[0]
        if len(group) > 1:
            ids = [u.evidence_id for u in group]
            text = "\n".join(u.text for u in group)
            digest = content_hash(text.encode())
            eid = stable_id("ev", VERSION, root.doc_id, *ids, digest)
            boxes = [u.locator.bbox for u in group if u.locator.bbox]
            bbox = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                    max(b[2] for b in boxes), max(b[3] for b in boxes)) if boxes else None
            root = root.model_copy(update={"evidence_id": eid, "text": text, "text_hash": digest,
                                          "locator": root.locator.model_copy(update={"source_ids": ids, "bbox": bbox})})
            out.extend(u.model_copy(update={"parent_id": eid,
                                            "locator": u.locator.model_copy(update={"context_only": True})}) for u in group)
        if FIGURE_ONLY.fullmatch(root.text):
            continue
        if len(root.text) <= max_chars or root.block_type == "footer":
            out.append(root)
            continue
        children = []
        spans = list(_chunk_spans(root.text, max_chars))
        if len(spans) <= 1:
            out.append(root)
            continue
        for start, end in spans:
            if not (text := root.text[start:end]).strip():
                continue
            digest = content_hash(text.encode())
            children.append(root.model_copy(update={
                "evidence_id": stable_id("ev", VERSION, root.doc_id, root.evidence_id, str(start), str(end), digest),
                "text": text, "text_hash": digest, "parent_id": root.evidence_id,
                "locator": root.locator.model_copy(update={"source_ids": root.locator.source_ids or [root.evidence_id],
                                                           "span_start": start, "span_end": end}),
            }))
        out.append(root.model_copy(update={"child_ids": [c.evidence_id for c in children]}))
        out.extend(children)
    return out
