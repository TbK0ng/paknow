"""Native EPUB parser: OPF spine order + NCX chapter tree + figure anchoring.

Keeps the original XHTML structure. References are located by
"book -> chapter -> figure/anchor", never by an invented page number.
"""
from __future__ import annotations

import posixpath
import zipfile
from dataclasses import dataclass, field
from urllib.parse import unquote

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from lxml import etree

import warnings

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

from paknow.evidence.contracts import (
    DocFormat,
    EvidenceUnit,
    SourceLocator,
)
from paknow.ids import content_hash, stable_id
from paknow.ingest.figure_labels import FIGURE_LABEL

OPF_NS = {"opf": "http://www.idpf.org/2007/opf", "dc": "http://purl.org/dc/elements/1.1/"}
NCX_NS = {"ncx": "http://www.daisy.org/z3986/2005/ncx/"}


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


def parse_epub(path: str, doc_id: str) -> EpubParseResult:
    zf = zipfile.ZipFile(path)
    try:
        opf_path = _find_opf(zf)
        meta = _parse_opf(zf, opf_path)
        toc = _parse_ncx(zf)
        href_paths: dict[str, list[str]] = {}
        sections: dict[str, dict[str, list[str]]] = {}
        for entry in toc:
            if href := entry["href"]:
                if fragment := entry["fragment"]:
                    sections.setdefault(href, {}).setdefault(fragment, entry["path"])
                else:
                    href_paths.setdefault(href, entry["path"])

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
            chapter_path = href_paths.get(href, [])
            try:
                html = zf.read(href).decode("utf-8", "ignore")
            except KeyError:
                continue
            result.evidence_units.extend(_parse_xhtml(html, href, doc_id, chapter_path, sections=sections.get(href)))
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
                    _walk_nav(np, name, 1, [], out)
            return out
    return []


def _walk_nav(np, ncx: str, level: int, path: list[str], out: list[dict]) -> None:
    label = _text(np.find("ncx:navLabel/ncx:text", NCX_NS))
    src = None
    content = np.find("ncx:content", NCX_NS)
    if content is not None:
        src = content.attrib.get("src")
    cur = path + ([label] if label else [])
    href, _, fragment = (src or "").partition("#")
    out.append({"title": label, "href": _resolve(ncx, href) if href else None,
                "fragment": unquote(fragment) or None, "level": level, "path": list(cur)})
    for child in np.findall("ncx:navPoint", NCX_NS):
        _walk_nav(child, ncx, level + 1, cur, out)


# --------------------------------------------------------------------------- #
# XHTML -> text evidence (captions do not shift the body paragraph sequence)
# --------------------------------------------------------------------------- #
def _parse_xhtml(html: str, href: str, doc_id: str, chapter_path: list[str], *,
                 sections: dict[str, list[str]] | None = None) -> list[EvidenceUnit]:
    soup = BeautifulSoup(html, "lxml")
    body = soup.body or soup
    positions: dict[int, int] = {}
    changes = []
    for i, tag in enumerate(body.find_all(True) if sections else []):
        positions[id(tag)] = i
        key = tag.get("id") or tag.get("name")
        if key in sections:
            changes.append((i, sections[key]))
    change = 0

    units: list[EvidenceUnit] = []
    idx = 0
    caption_idx = 0
    elements = body.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "li", "figcaption", "div"])
    for el in elements:
        cls = " ".join(el.get("class", []))
        in_figure_div = el.find_parent("div", class_=lambda c: c and "figure" in c) is not None
        is_caption = el.name == "figcaption" or "caption" in cls
        if el.name == "div" and not is_caption:
            continue
        if in_figure_div and not is_caption:
            continue
        if not is_caption and el.find_parent(["figcaption", "div"], class_=lambda c: c and "caption" in c):
            continue
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        block_type = "paragraph"
        if is_caption:
            block_type = "caption"
        elif el.name in ("h1", "h2", "h3", "h4", "h5", "h6") or "title" in cls or "head" in cls:
            block_type = "heading"

        if in_figure_div or el.name in ("figcaption", "div"):
            evidence_id = stable_id("ev", doc_id, href, "caption", str(caption_idx))
            caption_idx += 1
        else:
            evidence_id = stable_id("ev", doc_id, href, str(idx))
            idx += 1
        anchor = el.get("id") or el.get("name")
        if not anchor and (link := el.find("a", id=True)) is not None:
            anchor = link["id"]
        if not anchor and (link := el.find("a", attrs={"name": True})) is not None:
            anchor = link["name"]
        position = positions.get(id(el), -1)
        while change < len(changes) and changes[change][0] <= position:
            chapter_path = changes[change][1]
            change += 1
        active_path = sections.get(anchor, chapter_path) if sections else chapter_path
        label = FIGURE_LABEL.search(text) if is_caption else None
        units.append(
            EvidenceUnit(
                evidence_id=evidence_id,
                doc_id=doc_id,
                locator=SourceLocator(doc_id=doc_id, format=DocFormat.epub, epub_href=href,
                                      epub_fragment=anchor, figure_label=label.group() if label else None,
                                      chapter_path=active_path),
                text=text,
                text_hash=content_hash(text.encode("utf-8")),
                block_type=block_type,  # type: ignore[arg-type]
                chapter_path=active_path,
            )
        )
    return units


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _text(el) -> str:
    if el is None:
        return ""
    return (el.text or "").strip()


def _resolve(base: str, rel: str) -> str:
    return posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(rel)))
