"""Build, verify, and optionally publish the PAknow MCP release.

Publishing is gated on:
1. Unit/e2e tests passing.
2. A clean git working tree.
3. Release bundle verification passing.
4. ``gh`` authentication succeeding.
5. The release tag not already existing.

Usage:
    python scripts/release.py                  # build + verify only
    python scripts/release.py --publish        # build + verify + tag + GitHub release
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent.parent
DIST = ROOT / "dist"


def run(args: list[str], *, cwd: Path, timeout: int = 1200) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def require_ok(result: subprocess.CompletedProcess[str], label: str) -> str:
    if result.returncode != 0:
        raise SystemExit(
            f"{label} failed (exit {result.returncode})\n"
            f"stdout:\n{result.stdout[-6000:]}\n"
            f"stderr:\n{result.stderr[-6000:]}"
        )
    return result.stdout.strip()


def read_version() -> str:
    import re

    text = (ROOT / "src" / "paknow_mcp" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit("Cannot find package version")
    return match.group(1)


def run_tests() -> None:
    probe = run(
        [sys.executable, "-c", "import pytest, mcp, httpx; print('ok')"],
        cwd=ROOT,
    )
    if probe.returncode == 0:
        result = run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT)
    else:
        result = run(
            [
                "uv",
                "run",
                "--with",
                "pytest>=8",
                "--with",
                "pytest-asyncio>=0.24",
                "--with",
                "mcp>=2,<3",
                "--with",
                "httpx>=0.27",
                "python",
                "-m",
                "pytest",
                "-q",
            ],
            cwd=ROOT,
        )
    output = require_ok(result, "tests")
    print(output.splitlines()[-1] if output else "tests passed")


def build() -> tuple[Path, Path, Path]:
    result = run([sys.executable, str(ROOT / "scripts" / "build_release.py")], cwd=ROOT)
    require_ok(result, "build_release")
    version = read_version()
    release_dir = DIST / "release" / f"paknow-mcp-{version}"
    archive = DIST / f"paknow-mcp-{version}.zip"
    wheel = next(release_dir.joinpath("dist").glob("*.whl"))
    sdist = next(release_dir.joinpath("dist").glob("*.tar.gz"))
    return archive, wheel, sdist


def verify() -> None:
    result = run(
        [sys.executable, str(ROOT / "scripts" / "verify_release.py")],
        cwd=ROOT,
        timeout=1800,
    )
    require_ok(result, "verify_release")
    # The verifier prints a large JSON report; keep the final status visible.
    print("release verification passed")


def git(*args: str) -> str:
    result = run(["git", *args], cwd=REPO_ROOT)
    return require_ok(result, f"git {' '.join(args)}")


def ensure_clean() -> None:
    status = git("status", "--porcelain", "--untracked-files=normal")
    if status:
        raise SystemExit(
            "Refusing to publish from a dirty working tree. Commit the release changes first.\n"
            + status
        )


def ensure_gh() -> None:
    result = run(["gh", "auth", "status"], cwd=REPO_ROOT)
    require_ok(result, "gh auth status")
    remote = run(["git", "remote", "get-url", "origin"], cwd=REPO_ROOT)
    require_ok(remote, "git remote get-url origin")


def publish(archive: Path, wheel: Path, sdist: Path, version: str, tag: str) -> None:
    ensure_clean()
    ensure_gh()
    existing = run(["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"], cwd=REPO_ROOT)
    if existing.returncode == 0:
        raise SystemExit(f"Tag {tag} already exists; bump the version or choose another tag.")

    release_dir = DIST / "release" / f"paknow-mcp-{version}"
    checksums = release_dir / "SHA256SUMS"
    notes = _notes(version, tag)

    git("tag", "-a", tag, "-m", f"PAknow MCP {version}")
    git("push", "origin", tag)

    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".md", delete=False
    ) as handle:
        handle.write(notes)
        notes_path = Path(handle.name)
    try:
        result = run(
            [
                "gh",
                "release",
                "create",
                tag,
                str(archive),
                str(wheel),
                str(sdist),
                str(checksums),
                "--verify-tag",
                "--title",
                f"PAknow MCP {version}",
                "--notes-file",
                str(notes_path),
            ],
            cwd=REPO_ROOT,
            timeout=600,
        )
        require_ok(result, "gh release create")
    finally:
        notes_path.unlink(missing_ok=True)

    print(f"Published {tag}")
    print(result.stdout.strip())


def _notes(version: str, tag: str) -> str:
    manifest_path = DIST / "release" / f"paknow-mcp-{version}" / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    commit = manifest.get("git_commit") or "unknown"
    return f"""# PAknow MCP {version}

Evidence-first MCP adapter for the PAknow price-action knowledge base.

## Install

```bash
pip install paknow-mcp=={version}
```

Requires a running PAknow service:

```bash
uvicorn apps.api.main:app --port 8000
```

## Tools

- `paknow_ask` — grounded answer with citations
- `paknow_chart` — chart analysis with optional right-edge cutoff
- `paknow_search` — retrieval-only evidence search
- `paknow_evidence` — full evidence by ID
- `paknow_documents` / `paknow_source` — corpus metadata
- `paknow_page_image` / `paknow_figure_image` — original images
- `paknow_run` / `paknow_sops` — audit and memory state
- `paknow_health` — service health

## Skill

The bundle includes `skill/paknow/SKILL.md`, a client-neutral Agent Skill covering
intent routing, capability boundaries, citation fidelity, and failure behavior.

## Verification

Built from commit `{commit}` and verified by extracting the bundle into `.omp/`,
installing the wheel in a fresh virtualenv, and exercising every MCP tool over
stdio against a deterministic PAknow mock. See `VERIFICATION.json` in the build
workspace and `SHA256SUMS` in this release.

Tag: `{tag}`
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true", help="Tag and create the GitHub release after verification.")
    parser.add_argument("--skip-tests", action="store_true", help="Skip the package test suite (not recommended).")
    parser.add_argument("--skip-verify", action="store_true", help="Skip bundle verification (not recommended).")
    parser.add_argument("--tag", default=None, help="Release tag (default: paknow-mcp-v<version>).")
    args = parser.parse_args()

    version = read_version()
    tag = args.tag or f"paknow-mcp-v{version}"

    if not args.skip_tests:
        run_tests()
    archive, wheel, sdist = build()
    if not args.skip_verify:
        verify()

    if args.publish:
        publish(archive, wheel, sdist, version, tag)
    else:
        print(f"Built and verified paknow-mcp {version} ({tag}); pass --publish to publish.")


if __name__ == "__main__":
    main()
