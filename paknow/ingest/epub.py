"""Native EPUB parser: OPF spine order + NCX chapter tree + figure anchoring.

Keeps the original XHTML structure. References are located by
"book -> chapter -> figure/anchor", never by an invented page number.
"""
from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from urllib.parse import unquote

from bs4 import BeautifulSoup, Tag, XMLParsedAsHTMLWarning
from lxml import etree

import warnings

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

from paknow.evidence.contracts import (
    DocFormat,
    EvidenceUnit,
    FigureAsset,
    SourceLocator,
)
from paknow.ids import content_hash, stable_id
from paknow.ingest.images import image_size
from paknow.ingest.store import write_object

OPF_NS = {"opf": "http://www.idpf.org/2007/opf", "dc": "http://purl.org/dc/elements/1.1/"}
NCX_NS = {"ncx": "http://www.daisy.org/z3986/2005/ncx/"}
_FIGURE_LABEL_RE = re.compile(r"Figure\s+[A-Za-z0-9]+(?:\.[0-9]+)?", re.IGNORECASE)


@dataclass
class _OpfMeta:
    title: str
    author: str
    language: str
    spine_hrefs: list[str]


@dataclass
class EpubParseResult:
    doc_id: str
    title: str
    author: str
    language: str
    entry_count: int
    toc: list[dict] = field(default_factory=list)
    evidence_units: list[EvidenceUnit] = field(default_factory=list)
    figures: list[FigureAsset] = field(default_factory=list)


def parse_epub(path: str, doc_id: str) -> EpubParseResult:
    zf = zipfile.ZipFile(path)
    try:
        opf_path = _find_opf(zf)
        meta = _parse_opf(zf, opf_path)
        toc = _parse_ncx(zf)
        href_to_path = {_norm(t["href"]): t["path"] for t in toc if t.get("href")}

        result = EpubParseResult(
            doc_id=doc_id,
            title=meta.title,
            author=meta.author,
            language=meta.language,
            entry_count=len(meta.spine_hrefs),
            toc=toc,
        )
        for href in meta.spine_hrefs:
            if not href.lower().endswith((".htm", ".html", ".xhtml")):
                continue
            chapter_path = href_to_path.get(_norm(href), [])
            try:
                html = zf.read(href).decode("utf-8", "ignore")
            except KeyError:
                continue
            units, figures = _parse_xhtml(html, href, doc_id, chapter_path, zf)
            result.evidence_units.extend(units)
            result.figures.extend(figures)
        return result
    finally:
        zf.close()


# --------------------------------------------------------------------------- #
# OPF / NCX
# --------------------------------------------------------------------------- #
def _find_opf(zf: zipfile.ZipFile) -> str:
    try:
        xml = etree.fromstring(zf.read("META-INF/container.xml"))
        rootfiles = xml.findall(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
        for rf in rootfiles:
            if "full-path" in rf.attrib:
                return rf.attrib["full-path"]
    except KeyError:
        pass
    # fallback
    for name in zf.namelist():
        if name.endswith(".opf"):
            return name
    raise ValueError("EPUB has no OPF")


def _parse_opf(zf: zipfile.ZipFile, opf_path: str) -> _OpfMeta:
    root = etree.fromstring(zf.read(opf_path))
    title = _text(root.find(".//dc:title", OPF_NS))
    author = _text(root.find(".//dc:creator", OPF_NS))
    lang = _text(root.find(".//dc:language", OPF_NS))

    manifest = {}
    for item in root.findall(".//opf:item", OPF_NS):
        if "id" in item.attrib and "href" in item.attrib:
            manifest[item.attrib["id"]] = item.attrib["href"]

    spine_hrefs = []
    for ref in root.findall(".//opf:itemref", OPF_NS):
        href = manifest.get(ref.attrib.get("idref", ""))
        if href:
            spine_hrefs.append(_resolve(opf_path, href))
    return _OpfMeta(title=title, author=author, language=lang, spine_hrefs=spine_hrefs)


def _parse_ncx(zf: zipfile.ZipFile) -> list[dict]:
    for name in zf.namelist():
        if name.endswith(".ncx"):
            root = etree.fromstring(zf.read(name))
            navmap = root.find(".//ncx:navMap", NCX_NS)
            out: list[dict] = []
            if navmap is not None:
                for np in navmap.findall("ncx:navPoint", NCX_NS):
                    _walk_nav(np, 1, [], out)
            return out
    return []


def _walk_nav(np, level: int, path: list[str], out: list[dict]) -> None:
    label = _text(np.find("ncx:navLabel/ncx:text", NCX_NS))
    src = None
    content = np.find("ncx:content", NCX_NS)
    if content is not None:
        src = content.attrib.get("src")
    cur = path + ([label] if label else [])
    out.append({"title": label, "href": _norm(src) if src else None, "level": level, "path": list(cur)})
    for child in np.findall("ncx:navPoint", NCX_NS):
        _walk_nav(child, level + 1, cur, out)


# --------------------------------------------------------------------------- #
# XHTML -> evidence units + figures
# --------------------------------------------------------------------------- #
def _parse_xhtml(
    html: str, href: str, doc_id: str, chapter_path: list[str], zf: zipfile.ZipFile
) -> tuple[list[EvidenceUnit], list[FigureAsset]]:
    soup = BeautifulSoup(html, "lxml")
    body = soup.body or soup

    figures: list[FigureAsset] = []
    anchor_to_figure: dict[str, str] = {}
    # Pass A: figure divs
    for div in body.find_all("div", class_=lambda c: c and "figure" in c):
        fig = _extract_figure(div, href, doc_id, chapter_path, zf)
        if fig is not None:
            figures.append(fig)
            # figure anchor id (from the caption <a id="...">)
            a = div.find("a", id=True)
            if a is not None:
                anchor_to_figure[a["id"]] = fig.figure_id

    # Pass B: text blocks in reading order
    units: list[EvidenceUnit] = []
    idx = 0
    for el in _text_elements(body):
        cls = " ".join(el.get("class", [])) if isinstance(el, Tag) else ""
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        block_type = "paragraph"
        if el.name in ("h1", "h2", "h3", "h4", "h5", "h6") or "title" in cls or "head" in cls:
            block_type = "heading"
        elif "caption" in cls:
            block_type = "caption"

        # figure references from <a href="#anchor">
        fig_ids: list[str] = []
        for a in el.find_all("a", href=True):
            target = a["href"].lstrip("#")
            if target in anchor_to_figure:
                fid = anchor_to_figure[target]
                if fid not in fig_ids:
                    fig_ids.append(fid)

        evidence_id = stable_id("ev", doc_id, href, str(idx))
        idx += 1
        units.append(
            EvidenceUnit(
                evidence_id=evidence_id,
                doc_id=doc_id,
                locator=SourceLocator(doc_id=doc_id, format=DocFormat.epub, epub_href=href),
                text=text,
                text_hash=content_hash(text.encode("utf-8")),
                block_type=block_type,  # type: ignore[arg-type]
                chapter_path=chapter_path,
                figure_ids=fig_ids,
            )
        )
    return units, figures


def _text_elements(body):
    """Yield heading/paragraph/caption leaf elements in document order.

    Container ``<div>`` (e.g. ``div.story`` wrapping a whole chapter) must NOT
    be yielded — otherwise a single unit captures the entire chapter.
    """
    for child in body.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "li"]):
        if child.find_parent("div", class_=lambda c: c and "figure" in c) is not None:
            continue
        yield child


def _extract_figure(
    div, href: str, doc_id: str, chapter_path: list[str], zf: zipfile.ZipFile
) -> FigureAsset | None:
    img = div.find("img")
    if img is None:
        return None
    src = img.get("src", "")
    if not src:
        return None
    full_src = _resolve(href, src)
    try:
        raw = zf.read(full_src)
    except KeyError:
        return None
    try:
        w, h = image_size(raw)
    except ValueError:
        w, h = 0, 0

    # figure anchor + label
    a = div.find("a", id=True)
    anchor_id = a["id"] if a is not None else None
    caption = div.find(class_=lambda c: c and "caption" in c)
    caption_text = caption.get_text(" ", strip=True) if caption is not None else ""
    m = _FIGURE_LABEL_RE.search(caption_text)
    figure_label = m.group(0) if m else None

    figure_id = stable_id("fig", doc_id, anchor_id or full_src)
    write_object(f"epub/{doc_id}/{full_src}", raw)
    return FigureAsset(
        figure_id=figure_id,
        doc_id=doc_id,
        locator=SourceLocator(
            doc_id=doc_id,
            format=DocFormat.epub,
            epub_href=href,
            chapter_path=chapter_path,
            figure_label=figure_label,
        ),
        image_hash=content_hash(raw),
        rel_path=f"epub/{doc_id}/{full_src}",
        width_px=w,
        height_px=h,
        annotation_source="native",
    )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _text(el) -> str:
    if el is None:
        return ""
    return (el.text or "").strip()


def _norm(href: str) -> str:
    return unquote(href).split("#")[0]


def _resolve(base: str, rel: str) -> str:
    return posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(rel)))
