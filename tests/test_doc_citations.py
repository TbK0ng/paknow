"""Every ``path:line`` citation in the docs must resolve.

The book cites code by location 58 times. A citation that points past the end
of a file, or at a bare filename with no directory, is mechanically wrong and
was already shipped once (``tokenize.py:54-87`` on a 73-line file). This checks
the pointer, not the prose: whether the line still *says* what the chapter
claims stays a review job.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = sorted((ROOT / "book").glob("*.md")) + [ROOT / "README.md"]
CITATION = re.compile(r"`([\w./-]+\.(?:py|yml|yaml|toml|md|json|css|ini)):(\d+)(?:-(\d+))?`")


def citations() -> list[tuple[Path, str, int, int | None]]:
    found = []
    for doc in DOCS:
        for match in CITATION.finditer(doc.read_text(encoding="utf-8")):
            path, start, end = match.group(1), int(match.group(2)), match.group(3)
            found.append((doc.relative_to(ROOT), path, start, int(end) if end else None))
    return found


def test_citations_exist():
    # Guards the regex: a sweep that matches nothing would make the checks below vacuous.
    assert len(citations()) >= 40


@pytest.mark.parametrize("doc,path,start,end", citations())
def test_citation_resolves_to_a_line(doc: Path, path: str, start: int, end: int | None):
    target = ROOT / path
    assert target.is_file(), f"{doc}: {path} is not a repo-relative file"

    lines = len(target.read_text(encoding="utf-8", errors="ignore").splitlines())
    last = end or start
    assert last <= lines, f"{doc}: {path}:{start}{f'-{end}' if end else ''} but file has {lines} lines"
    assert start >= 1
