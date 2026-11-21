"""PDF parser: native text layer, bookmarks and source coordinates only.

Per plan §5.2: keep ``pdf_page_1based``, ``printed_page_label``,
``chapter_path`` and ``figure_label`` distinct; mark low-quality pages for OCR;
exclude header/footer/watermark blocks from semantic text while preserving the
original page.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import pymupdf as fitz

from paknow.evidence.contracts import (
    DocFormat,
    EvidenceUnit,
    SourceLocator,
)
from paknow.ids import content_hash, stable_id
from paknow.ingest.figure_labels import FIGURE_LABEL

_PAGE_LABEL_RE = re.compile(r"^\s*(\d{1,4})\s*$")
_HEADER_FOOTER_MARGIN = 0.08


@dataclass
class PdfParseResult:
    doc_id: str
    page_count: int
    toc: list[dict] = field(default_factory=list)
    evidence_units: list[EvidenceUnit] = field(default_factory=list)
    ocr_pages: list[int] = field(default_factory=list)
    page_labels: dict[int, str] = field(default_factory=dict)


def parse_pdf(
    path: str,
    doc_id: str,
    *,
    page_range: tuple[int, int] | None = None,
) -> PdfParseResult:
    doc = fitz.open(path)
    try:
        page_count = doc.page_count
        lo, hi = page_range or (0, page_count)
        hi = min(hi, page_count)

        toc = [{"level": lv, "title": t, "page": p} for lv, t, p in doc.get_toc()]
        chapter_paths = _page_chapter_paths(toc, page_count)
        page_labels = _pdf_page_labels(doc, page_count)

        result = PdfParseResult(doc_id=doc_id, page_count=page_count, toc=toc)
        result.page_labels = {k: v for k, v in page_labels.items() if v is not None}

        for pno in range(lo, hi):
            page = doc[pno]
            units = _parse_page(page, pno, doc_id, chapter_paths[pno], page_labels.get(pno))
            # OCR candidate: page with little native text
            native_text = sum(1 for u in units if u.block_type == "paragraph")
            if native_text < 3 and page.get_text().strip() == "":
                result.ocr_pages.append(pno)
            result.evidence_units.extend(units)

        return result
    finally:
        doc.close()


# --------------------------------------------------------------------------- #
def _parse_page(page, pno: int, doc_id: str, chapter_path: list[str],
                printed_page: str | None) -> list[EvidenceUnit]:
    units: list[EvidenceUnit] = []
    h = page.rect.height

    # Preserve proximity-based caption detection with geometry, not raster data.
    image_bboxes = [info["bbox"] for info in page.get_image_info()]
    d = page.get_text("dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)

    # first pass: collect text blocks + sizes to compute the page body size
    text_blocks: list[tuple[dict, str, list[float]]] = []
    all_sizes: list[float] = []
    for block in d.get("blocks", []):
        if block.get("type") != 0:
            continue
        text, sizes, _flags = _flatten_block(block)
        if not text.strip():
            continue
        text_blocks.append((block, text, sizes))
        all_sizes.extend(sizes)
    median_size = _median(all_sizes) if all_sizes else 0.0

    for block, text, sizes in text_blocks:
        x0, y0, x1, y1 = block["bbox"]
        in_margin = y0 > (1 - _HEADER_FOOTER_MARGIN) * h or y1 < _HEADER_FOOTER_MARGIN * h

        if in_margin:
            block_type = "footer"
        elif _near_image((x0, y0, x1, y1), image_bboxes) or _looks_like_caption(text):
            block_type = "caption"
        elif _looks_like_heading(text, sizes, median_size):
            block_type = "heading"
        else:
            block_type = "paragraph"

        figure_label = FIGURE_LABEL.match(text.lstrip()) if block_type == "caption" else None

        units.append(
            EvidenceUnit(
                evidence_id=stable_id("ev", doc_id, str(pno), str(len(units))),
                doc_id=doc_id,
                locator=SourceLocator(
                    doc_id=doc_id,
                    format=DocFormat.pdf,
                    pdf_page_1based=pno + 1,
                    printed_page_label=printed_page,
                    figure_label=figure_label.group().strip() if figure_label else None,
                    chapter_path=chapter_path,
                    bbox=(x0, y0, x1, y1),
                ),
                text=text,
                text_hash=content_hash(text.encode("utf-8")),
                block_type=block_type,  # type: ignore[arg-type]
            )
        )

    return units


def _flatten_block(block) -> tuple[str, list[float], list[bool]]:
    lines: list[str] = []
    sizes: list[float] = []
    flags: list[bool] = []
    for line in block.get("lines", []):
        line_text = ""
        for span in line.get("spans", []):
            t = span.get("text", "")
            line_text += t
            if t.strip():
                sizes.append(span.get("size", 0.0))
                flags.append(bool(span.get("flags", 0) & 2**4))  # bold bit
        lines.append(line_text)
    return "\n".join(lines).strip(), sizes, flags


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _page_chapter_paths(toc: list[dict], page_count: int) -> list[list[str]]:
    paths: list[list[str]] = [[] for _ in range(page_count)]
    if not toc:
        return paths
    norm = sorted(
        ((e["level"], e["title"], e["page"] - 1) for e in toc if e["page"] >= 1),
        key=lambda x: x[2],
    )
    stack: list[tuple[int, str]] = []
    for level, title, p in norm:
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        if 0 <= p < page_count:
            paths[p] = [t for _, t in stack]
    cur: list[str] = []
    for i in range(page_count):
        if paths[i]:
            cur = paths[i]
        else:
            paths[i] = list(cur)
    return paths


def _pdf_page_labels(doc, page_count: int) -> dict[int, str]:
    """Printed (chapter-local) page labels from the footer number line."""
    labels: dict[int, str] = {}
    for i in range(page_count):
        page = doc[i]
        h = page.rect.height
        for block in page.get_text("blocks"):
            if block[6] != 0:
                continue
            x0, y0, x1, y1, text = block[:5]
            if y0 > (1 - _HEADER_FOOTER_MARGIN) * h:
                for line in text.splitlines():
                    m = _PAGE_LABEL_RE.match(line.strip())
                    if m:
                        labels[i] = m.group(1)
                        break
                if i in labels:
                    break
    return labels


def _near_image(bbox, image_bboxes, tol: float = 12.0) -> bool:
    x0, y0, x1, y1 = bbox
    for ix0, iy0, ix1, iy1 in image_bboxes:
        # caption directly below or above an image region
        if abs(y0 - iy1) < tol or abs(y1 - iy0) < tol:
            if not (x1 < ix0 - tol or x0 > ix1 + tol):
                return True
    return False


def _looks_like_caption(text: str) -> bool:
    t = text.strip()
    return bool(re.match(r"^(图\s*\d|Figure\s+\d|fig\.\s*\d|图表)", t, re.IGNORECASE))


def _looks_like_heading(text: str, sizes: list[float], median_size: float) -> bool:
    t = text.strip()
    if len(t) > 120:
        return False
    if re.match(r"^(第[一二三四五六七八九十百\d]+[章节篇部]|[0-9]+(\.[0-9]+)*\s+\S)", t):
        return True
    if sizes and median_size > 0:
        avg = sum(sizes) / len(sizes)
        if avg > 1.3 * median_size and not t.endswith(("。", ".", "，", ",")):
            return True
    return False


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n % 2:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0
