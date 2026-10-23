#!/usr/bin/env python3
"""Validate the local markdown issue tracker under ``.scratch/``.

The tracker is plain markdown, so the contract has to be checked rather than
enforced by a schema. This script is the check: it runs in pre-commit and CI
and is stdlib-only, so a hook checkout never needs project dependencies.

Rules (one markdown file = one ticket or spec):

- exactly one ``**Status:** <value>`` (``Status: <value>`` also accepted) with a
  value from ``STATUSES``;
- every ``**Blocked by:** 01, 02`` reference names an ``NN-*.md`` in the same
  feature directory, and a ticket that is ``ready-for-agent``,
  ``ready-for-human`` or ``claimed`` has every blocker ``resolved``;
- an optional ``**Review base:** <sha>`` is 7-40 hex characters.

Failures print as ``path:line: message`` and exit 1.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

STATUSES = (
    "needs-triage",
    "needs-info",
    "ready-for-agent",
    "ready-for-human",
    "wontfix",
    "claimed",
    "resolved",
)

#: Statuses that promise the ticket can be picked up right now.
_ACTIONABLE = frozenset({"ready-for-agent", "ready-for-human", "claimed"})

_STATUS_RE = re.compile(r"^\s*(?:\*\*Status:\*\*|Status:)\s*(\S+)\s*$")
_BLOCKED_RE = re.compile(r"^\s*(?:\*\*Blocked by:\*\*|Blocked by:)\s*(.*?)\s*$")
_REVIEW_RE = re.compile(r"^\s*(?:\*\*Review base:\*\*|Review base:)\s*(.*?)\s*$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_ISSUE_NUMBER_RE = re.compile(r"^(\d+)-")


@dataclass
class Ticket:
    """One parsed markdown file plus the issue numbers it depends on."""

    path: Path
    status: str | None = None
    status_line: int | None = None
    blockers: list[int] = field(default_factory=list)
    blocker_line: int | None = None
    review_base: str | None = None
    errors: list[tuple[int, str]] = field(default_factory=list)

    @property
    def issues_dir(self) -> Path:
        return self.path.parent if self.path.parent.name == "issues" else self.path.parent / "issues"


def discover(root: Path) -> list[Path]:
    """Return every spec and issue ticket under ``root/.scratch``, sorted."""
    scratch = root / ".scratch"
    if not scratch.is_dir():
        return []
    paths = list(scratch.glob("*/spec.md"))
    paths += [path for path in scratch.glob("*/issues/*.md") if path.is_file()]
    return sorted(paths)


def parse(path: Path) -> Ticket:
    """Parse one file, collecting syntax errors with the line they occur on."""
    ticket = Ticket(path)
    status_count = 0

    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        status = _STATUS_RE.match(line)
        if status:
            status_count += 1
            value = status.group(1).strip("`")
            if status_count > 1:
                ticket.errors.append(
                    (lineno, f"duplicate Status (first at line {ticket.status_line})")
                )
                continue
            ticket.status = value
            ticket.status_line = lineno
            if value not in STATUSES:
                ticket.errors.append(
                    (
                        lineno,
                        f"unknown Status {value!r}; expected one of: {', '.join(STATUSES)}",
                    )
                )
            continue

        blocked = _BLOCKED_RE.match(line)
        if blocked:
            if ticket.blocker_line is None:
                ticket.blocker_line = lineno
            ticket.blockers += [int(number) for number in re.findall(r"\d+", blocked.group(1))]
            continue

        review = _REVIEW_RE.match(line)
        if review:
            ticket.review_base = review.group(1).strip("`")
            if not _SHA_RE.match(ticket.review_base):
                ticket.errors.append(
                    (
                        lineno,
                        f"invalid Review base {ticket.review_base!r}; "
                        "expected 7-40 hex characters",
                    )
                )

    if status_count == 0:
        ticket.errors.append(
            (1, "missing Status: add `**Status:** <value>` near the top")
        )

    return ticket


def _display(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def check_root(root: Path) -> list[str]:
    """Return every failure as ``path:line: message``; empty means OK."""
    tickets = [parse(path) for path in discover(root)]
    by_number: dict[Path, dict[int, Ticket]] = {}
    for ticket in tickets:
        numbers = by_number.setdefault(ticket.issues_dir, {})
        match = _ISSUE_NUMBER_RE.match(ticket.path.name)
        if match:
            numbers[int(match.group(1))] = ticket

    failures: list[str] = []
    for ticket in tickets:
        shown = _display(ticket.path, root)
        failures += [f"{shown}:{lineno}: {message}" for lineno, message in ticket.errors]

        numbers = by_number.get(ticket.issues_dir, {})
        for blocker in ticket.blockers:
            line = ticket.blocker_line or 1
            target = numbers.get(blocker)
            if target is None:
                failures.append(
                    f"{shown}:{line}: blocked by {blocker:02d}, "
                    f"but no issues/{blocker:02d}-*.md exists"
                )
            elif ticket.status in _ACTIONABLE and target.status != "resolved":
                failures.append(
                    f"{shown}:{line}: blocked by {blocker:02d} "
                    f"(status: {target.status or 'missing'}), which is not resolved"
                )

    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root containing .scratch/ (default: repo root)",
    )
    args = parser.parse_args(argv)

    failures = check_root(args.root)
    if failures:
        for failure in failures:
            print(failure)
        return 1

    print(f"scratch: {len(discover(args.root))} file(s) OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
