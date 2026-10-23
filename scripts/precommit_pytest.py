#!/usr/bin/env python3
"""Run pytest through the repo venv from a pre-commit hook.

``language: system`` uses the first ``python`` on PATH, which may be a
uv-managed interpreter without the project's dev dependencies. This wrapper
only needs the stdlib: it locates the repo's venv (Windows or POSIX) and
delegates the actual run to it.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def venv_python(root: Path = ROOT) -> Path | None:
    """Return the repo venv's interpreter, or ``None`` when it is absent."""
    for candidate in (
        root / ".venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
    ):
        if candidate.is_file():
            return candidate
    return None


def main() -> int:
    python = venv_python()
    if python is None:
        print(
            "precommit_pytest: no .venv Python found; create .venv and install "
            "the dev dependencies",
            file=sys.stderr,
        )
        return 1
    return subprocess.call([str(python), "-m", "pytest", "-q"], cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
