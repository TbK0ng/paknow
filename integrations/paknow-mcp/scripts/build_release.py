"""Build a self-contained PAknow MCP release bundle.

Layout::

    dist/release/paknow-mcp-<version>/
      README.md
      MANIFEST.json
      SHA256SUMS
      dist/paknow_mcp-<version>-py3-none-any.whl
      dist/paknow_mcp-<version>.tar.gz
      skill/paknow/SKILL.md

The script does not publish anything. Use ``verify_release.py`` to extract and
test the bundle, and ``release.py`` to publish after verification passes.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent.parent
DIST = ROOT / "dist"
RELEASE_ROOT = DIST / "release"


def read_version() -> str:
    text = (ROOT / "src" / "paknow_mcp" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit("Cannot find __version__ in src/paknow_mcp/__init__.py")
    return match.group(1)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_value(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


def build_distributions(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "uv",
                "build",
                "--out-dir",
                str(out_dir),
                "--wheel",
                "--sdist",
                "--no-create-gitignore",
                str(ROOT),
            ],
            cwd=ROOT,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        subprocess.run(
            [sys.executable, "-m", "build", "--outdir", str(out_dir), str(ROOT)],
            cwd=ROOT,
            check=True,
        )
    artifacts = sorted(out_dir.glob("*.whl")) + sorted(out_dir.glob("*.tar.gz"))
    if not artifacts:
        raise SystemExit(f"No distributions were built in {out_dir}")
    return artifacts


def write_checksums(release_dir: Path) -> None:
    lines: list[str] = []
    for path in sorted(release_dir.rglob("*")):
        if not path.is_file() or path.name == "SHA256SUMS":
            continue
        rel = path.relative_to(release_dir).as_posix()
        lines.append(f"{sha256_file(path)}  {rel}")
    (release_dir / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_manifest(release_dir: Path, version: str, artifacts: list[Path]) -> None:
    files = []
    for path in sorted(release_dir.rglob("*")):
        if not path.is_file() or path.name in {"MANIFEST.json", "SHA256SUMS"}:
            continue
        files.append(
            {
                "path": path.relative_to(release_dir).as_posix(),
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
        )
    try:
        from importlib.metadata import version as dist_version

        mcp_version = dist_version("mcp")
    except Exception:
        mcp_version = None
    manifest = {
        "name": "paknow-mcp",
        "version": version,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "mcp_sdk": mcp_version,
        "git_commit": git_value("rev-parse", "HEAD"),
        "git_dirty": bool(git_value("status", "--porcelain")),
        "artifacts": [path.name for path in artifacts],
        "files": files,
    }
    (release_dir / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def make_zip(release_dir: Path, archive: Path) -> None:
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(release_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(release_dir.parent).as_posix()
            info = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())


def main() -> None:
    version = read_version()

    for stale in [
        RELEASE_ROOT,
        DIST / f"paknow-mcp-{version}.zip",
    ]:
        if stale.exists():
            shutil.rmtree(stale) if stale.is_dir() else stale.unlink()

    build_dir = DIST / "_build"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    artifacts = build_distributions(build_dir)

    release_dir = RELEASE_ROOT / f"paknow-mcp-{version}"
    (release_dir / "dist").mkdir(parents=True)
    (release_dir / "skill" / "paknow").mkdir(parents=True)

    for artifact in artifacts:
        shutil.copy2(artifact, release_dir / "dist" / artifact.name)
    shutil.copy2(ROOT / "README.md", release_dir / "README.md")
    shutil.copy2(
        ROOT / "skills" / "paknow" / "SKILL.md",
        release_dir / "skill" / "paknow" / "SKILL.md",
    )

    write_checksums(release_dir)
    write_manifest(release_dir, version, [release_dir / "dist" / a.name for a in artifacts])

    archive = DIST / f"paknow-mcp-{version}.zip"
    make_zip(release_dir, archive)

    print(f"Built paknow-mcp {version}")
    for artifact in artifacts:
        print(f"  {artifact.name}")
    print(f"  {archive}")
    print(f"  {release_dir / 'MANIFEST.json'}")


if __name__ == "__main__":
    main()
