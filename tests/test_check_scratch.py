"""The scratch tracker checker is a small CI/pre-commit gate over the local
markdown tickets: one canonical status per file, existing and satisfied
blockers, and a valid review base.

These tests pin the checker's observable contract (the `path:line: message`
lines and the exit code), not its internal parsing helpers.
"""
from __future__ import annotations

from pathlib import Path

from scripts import check_scratch


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _ticket(status: str | None = "resolved", blocked_by: str | None = None) -> str:
    lines = ["# 01: a ticket", ""]
    if blocked_by is not None:
        lines += [f"**Blocked by:** {blocked_by}", ""]
    if status is not None:
        lines += [f"**Status:** {status}", ""]
    lines += ["Body.", ""]
    return "\n".join(lines)


def _feature(tmp_path: Path) -> Path:
    return tmp_path / ".scratch" / "demo"


def test_missing_scratch_directory_is_ok(tmp_path):
    assert check_scratch.check_root(tmp_path) == []
    assert check_scratch.main(["--root", str(tmp_path)]) == 0


def test_bold_and_plain_status_are_accepted(tmp_path):
    feature = _feature(tmp_path)
    _write(feature / "issues" / "01-a.md", _ticket("resolved"))
    _write(feature / "spec.md", "# spec\n\nStatus: resolved\n")

    assert check_scratch.check_root(tmp_path) == []


def test_missing_status_is_reported_with_line(tmp_path):
    feature = _feature(tmp_path)
    _write(feature / "issues" / "01-a.md", "# 01\n\nno status here\n")

    failures = check_scratch.check_root(tmp_path)

    assert len(failures) == 1
    assert failures[0].startswith(".scratch/demo/issues/01-a.md:1: missing Status")
    assert check_scratch.main(["--root", str(tmp_path)]) == 1


def test_duplicate_status_is_reported(tmp_path):
    feature = _feature(tmp_path)
    _write(
        feature / "issues" / "01-a.md",
        "**Status:** resolved\n\n**Status:** ready-for-human\n",
    )

    failures = check_scratch.check_root(tmp_path)

    assert len(failures) == 1
    assert "duplicate Status (first at line 1)" in failures[0]


def test_unknown_status_lists_allowed_values(tmp_path):
    feature = _feature(tmp_path)
    _write(feature / "issues" / "01-a.md", _ticket("in-progress"))

    failures = check_scratch.check_root(tmp_path)

    assert len(failures) == 1
    assert "unknown Status 'in-progress'" in failures[0]
    assert "ready-for-agent" in failures[0]


def test_blocker_file_must_exist_in_the_same_feature(tmp_path):
    feature = _feature(tmp_path)
    _write(feature / "issues" / "01-a.md", _ticket("resolved", "02, 09"))
    _write(feature / "issues" / "02-b.md", _ticket("resolved"))

    failures = check_scratch.check_root(tmp_path)

    assert len(failures) == 1
    assert "blocked by 09" in failures[0]
    assert "no issues/09-*.md" in failures[0]


def test_ready_ticket_with_unresolved_blocker_fails(tmp_path):
    feature = _feature(tmp_path)
    _write(feature / "issues" / "01-a.md", _ticket("ready-for-agent", "02"))
    _write(feature / "issues" / "02-b.md", _ticket("claimed"))

    failures = check_scratch.check_root(tmp_path)

    assert len(failures) == 1
    assert "blocked by 02" in failures[0]
    assert "not resolved" in failures[0]


def test_ready_ticket_with_resolved_blockers_passes(tmp_path):
    feature = _feature(tmp_path)
    _write(feature / "issues" / "01-a.md", _ticket("ready-for-human", "02"))
    _write(feature / "issues" / "02-b.md", _ticket("resolved"))

    assert check_scratch.check_root(tmp_path) == []


def test_unresolved_blockers_are_allowed_for_other_statuses(tmp_path):
    feature = _feature(tmp_path)
    _write(feature / "issues" / "01-a.md", _ticket("needs-triage", "02"))
    _write(feature / "issues" / "02-b.md", _ticket("needs-info"))

    assert check_scratch.check_root(tmp_path) == []


def test_invalid_review_base_is_reported(tmp_path):
    feature = _feature(tmp_path)
    _write(
        feature / "spec.md",
        "**Status:** resolved\n**Review base:** not-a-sha\n",
    )

    failures = check_scratch.check_root(tmp_path)

    assert len(failures) == 1
    assert "invalid Review base 'not-a-sha'" in failures[0]


def test_empty_review_base_is_reported(tmp_path):
    feature = _feature(tmp_path)
    _write(feature / "spec.md", "**Status:** resolved\n**Review base:**\n")

    failures = check_scratch.check_root(tmp_path)

    assert len(failures) == 1
    assert "invalid Review base ''" in failures[0]


def test_relevant_review_base_passes(tmp_path):
    feature = _feature(tmp_path)
    _write(
        feature / "spec.md",
        "**Status:** resolved\n**Review base:** e06b7a4\n",
    )

    assert check_scratch.check_root(tmp_path) == []
