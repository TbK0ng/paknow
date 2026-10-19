"""Extract a release bundle under .omp and verify it end to end.

Checks:
1. Zip layout, MANIFEST.json, SHA256SUMS.
2. Wheel/sdist contents and metadata.
3. Skill frontmatter, tool references, and capability boundaries.
4. Fresh virtualenv install from the wheel.
5. Installed ``paknow-mcp`` stdio server against a deterministic mock PAknow API,
   covering every tool, images, structured output, citation normalization, and
   error paths.

Run with any Python that can execute ``uv`` (or ``python -m venv``):
    python integrations/paknow-mcp/scripts/verify_release.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent.parent
TESTS = ROOT / "tests"
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))
EXPECTED_TOOLS = {
    "paknow_ask",
    "paknow_chart",
    "paknow_search",
    "paknow_evidence",
    "paknow_documents",
    "paknow_source",
    "paknow_page_image",
    "paknow_figure_image",
    "paknow_run",
    "paknow_sops",
    "paknow_health",
}


@dataclass
class Checks:
    items: list[dict[str, object]] = field(default_factory=list)
    failures: int = 0
    warnings: list[str] = field(default_factory=list)

    def check(self, name: str, fn) -> None:
        started = time.perf_counter()
        try:
            detail = fn()
            self.items.append(
                {
                    "name": name,
                    "ok": True,
                    "detail": detail,
                    "ms": int((time.perf_counter() - started) * 1000),
                }
            )
        except Exception as exc:  # noqa: BLE001
            self.failures += 1
            self.items.append(
                {
                    "name": name,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "ms": int((time.perf_counter() - started) * 1000),
                }
            )

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd or REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def require_ok(result: subprocess.CompletedProcess[str], label: str) -> None:
    if result.returncode != 0:
        raise RuntimeError(
            f"{label} failed (exit {result.returncode})\n"
            f"stdout:\n{result.stdout[-4000:]}\n"
            f"stderr:\n{result.stderr[-4000:]}"
        )


def venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def create_venv(venv: Path) -> None:
    if venv.exists():
        shutil.rmtree(venv)
    if shutil.which("uv"):
        result = run(["uv", "venv", str(venv)])
        require_ok(result, "uv venv")
        return
    result = run([sys.executable, "-m", "venv", str(venv)])
    require_ok(result, "python -m venv")


def install_wheel(python: Path, wheel: Path) -> None:
    if shutil.which("uv"):
        result = run(["uv", "pip", "install", "--python", str(python), str(wheel)])
    else:
        result = run(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", str(wheel)]
        )
    require_ok(result, "install wheel")


def dependency_check(python: Path) -> None:
    if shutil.which("uv"):
        result = run(["uv", "pip", "check", "--python", str(python)])
    else:
        result = run([str(python), "-m", "pip", "check"])
    require_ok(result, "dependency check")


def safe_extract(archive: Path, target: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            name = member.filename
            if name.startswith("/") or ".." in Path(name).parts:
                raise RuntimeError(f"Unsafe archive member: {name}")
        zf.extractall(target)
    children = [p for p in target.iterdir() if p.is_dir()]
    if len(children) != 1:
        raise RuntimeError(f"Expected one top-level release directory, got {children}")
    return children[0]


def verify_checksums(release_dir: Path) -> int:
    sums = release_dir / "SHA256SUMS"
    if not sums.is_file():
        raise RuntimeError("SHA256SUMS is missing")
    count = 0
    for line in sums.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, rel = line.split("  ", 1)
        path = release_dir / rel
        if not path.is_file():
            raise RuntimeError(f"Checksum file missing: {rel}")
        actual = sha256_file(path)
        if actual != digest:
            raise RuntimeError(f"Checksum mismatch for {rel}: {actual} != {digest}")
        count += 1
    return count


def verify_manifest(release_dir: Path) -> dict:
    path = release_dir / "MANIFEST.json"
    if not path.is_file():
        raise RuntimeError("MANIFEST.json is missing")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("name") != "paknow-mcp":
        raise RuntimeError("Manifest name is not paknow-mcp")
    listed = {item["path"] for item in manifest.get("files", [])}
    required = {"README.md", "skill/paknow/SKILL.md"}
    missing = required - listed
    if missing:
        raise RuntimeError(f"Manifest is missing {sorted(missing)}")
    for item in manifest.get("files", []):
        target = release_dir / item["path"]
        if not target.is_file():
            raise RuntimeError(f"Manifest file missing: {item['path']}")
        if sha256_file(target) != item["sha256"]:
            raise RuntimeError(f"Manifest hash mismatch: {item['path']}")
    return manifest


def verify_wheel(wheel: Path, version: str) -> list[str]:
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
        required = {
            "paknow_mcp/__init__.py",
            "paknow_mcp/server.py",
            "paknow_mcp/client.py",
            "paknow_mcp/skills/paknow/SKILL.md",
        }
        missing = required - set(names)
        if missing:
            raise RuntimeError(f"Wheel missing {sorted(missing)}")
        forbidden = [
            n for n in names if n.startswith("tests/") or n.endswith(".env") or n.startswith("data/")
        ]
        if forbidden:
            raise RuntimeError(f"Wheel contains forbidden files: {forbidden[:5]}")
        metadata = next(n for n in names if n.endswith(".dist-info/METADATA"))
        text = zf.read(metadata).decode("utf-8", errors="replace")
        if f"Version: {version}" not in text:
            raise RuntimeError("Wheel metadata version mismatch")
        if "Requires-Dist: mcp" not in text:
            raise RuntimeError("Wheel metadata does not require mcp")
        entry_points = next(
            (n for n in names if n.endswith(".dist-info/entry_points.txt")), None
        )
        if entry_points is None:
            raise RuntimeError("Wheel has no entry_points.txt")
        entry_text = zf.read(entry_points).decode("utf-8", errors="replace")
        if "paknow-mcp = paknow_mcp.__main__:main" not in entry_text:
            raise RuntimeError("Wheel entry point is wrong")
    return names


def verify_sdist(sdist: Path) -> list[str]:
    with tarfile.open(sdist, "r:gz") as tf:
        names = tf.getnames()
    required_suffixes = [
        "src/paknow_mcp/server.py",
        "skills/paknow/SKILL.md",
        "pyproject.toml",
        "tests/mock_paknow.py",
    ]
    missing = [
        suffix for suffix in required_suffixes if not any(n.endswith(suffix) for n in names)
    ]
    if missing:
        raise RuntimeError(f"Sdist missing {missing}")
    return names


def verify_skill(skill: Path) -> dict[str, object]:
    text = skill.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise RuntimeError("Skill frontmatter is missing")
    front = text.split("---", 2)[1]
    name = re.search(r"^name:\s*(.+)$", front, re.M)
    description = re.search(r"^description:\s*(.+)$", front, re.M)
    if not name or name.group(1).strip() != "paknow":
        raise RuntimeError("Skill name must be paknow")
    if not description or len(description.group(1).strip()) < 80:
        raise RuntimeError("Skill description is missing or too short")
    mentioned = set(re.findall(r"\b(paknow_[a-z_]+)\b", text))
    unknown = mentioned - EXPECTED_TOOLS
    if unknown:
        raise RuntimeError(f"Skill mentions unknown tools: {sorted(unknown)}")
    # The skill is Chinese; check the substantive boundary tokens.
    for phrase in ["paknow_ask", "paknow_search", "paknow_evidence", "uncertainty", "行情", "引用"]:
        if phrase not in text:
            raise RuntimeError(f"Skill does not mention {phrase!r}")
    absolute = re.findall(r"[A-Za-z]:\\\\[^\s`]+", text)
    if absolute:
        raise RuntimeError(f"Skill contains absolute Windows paths: {absolute[:3]}")
    return {
        "bytes": len(text.encode("utf-8")),
        "mentioned_tools": sorted(mentioned),
    }


def parse_probe_output(stdout: str) -> dict:
    text = stdout.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError(f"Probe produced no JSON:\n{text[-4000:]}")
        return json.loads(text[start : end + 1])


def verify_installed(
    release_dir: Path,
    workdir: Path,
    checks: Checks,
) -> dict:
    wheel = next((release_dir / "dist").glob("*.whl"))
    venv = workdir / "venv"
    create_venv(venv)
    python = venv_python(venv)
    install_wheel(python, wheel)
    dependency_check(python)

    result = run([str(python), "-m", "paknow_mcp", "--version"])
    require_ok(result, "paknow-mcp --version")
    version_output = (result.stdout + result.stderr).strip()
    if "paknow-mcp" not in version_output:
        raise RuntimeError(f"Unexpected --version output: {version_output}")

    probe = workdir / "probe_release.py"
    shutil.copy2(ROOT / "scripts" / "probe_release.py", probe)

    from mock_paknow import MockPaknow

    mock = MockPaknow().start()
    try:
        env = {
            **os.environ,
            "PAKNOW_BASE_URL": mock.base_url,
            "PAKNOW_TIMEOUT": "15",
            "PYTHONUNBUFFERED": "1",
        }
        result = run(
            [str(python), str(probe)],
            cwd=workdir,
            env=env,
            timeout=300,
        )
        require_ok(result, "installed MCP probe")
        report = parse_probe_output(result.stdout)
    finally:
        mock.close()

    if not report.get("ok"):
        failed = [
            item for item in report.get("checks", []) if not item.get("ok")
        ]
        raise RuntimeError(f"Probe checks failed: {failed}")
    if set(report.get("tools", [])) != EXPECTED_TOOLS:
        raise RuntimeError("Probe tool set mismatch")
    checks.items.append(
        {
            "name": "installed_probe",
            "ok": True,
            "detail": {
                "checks": len(report.get("checks", [])),
                "tools": report.get("tools", []),
            },
        }
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive",
        type=Path,
        default=None,
        help="Release zip (default: newest integrations/paknow-mcp/dist/paknow-mcp-*.zip)",
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        default=REPO_ROOT / ".omp" / "release-verify",
        help="Extraction workspace (default: .omp/release-verify)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Accepted for compatibility; live PAknow smoke is not part of hermetic verification.",
    )
    args = parser.parse_args()
    del args.live

    archives = sorted((ROOT / "dist").glob("paknow-mcp-*.zip"))
    archive = args.archive or (archives[-1] if archives else None)
    if archive is None or not archive.is_file():
        raise SystemExit("No release archive found. Run scripts/build_release.py first.")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    workdir = args.workdir / stamp
    workdir.mkdir(parents=True, exist_ok=False)
    extracted = workdir / "extracted"

    checks = Checks()
    checks.check("archive_checksum", lambda: sha256_file(archive))
    release_dir: Path | None = None

    def extract_and_manifest() -> dict:
        nonlocal release_dir
        release_dir = safe_extract(archive, extracted)
        return verify_manifest(release_dir)

    checks.check("extract_and_manifest", extract_and_manifest)
    if release_dir is None:
        _finish(checks, workdir, {})
        return

    checks.check("checksums", lambda: verify_checksums(release_dir))
    manifest = json.loads((release_dir / "MANIFEST.json").read_text(encoding="utf-8"))
    version = str(manifest["version"])
    if manifest.get("git_dirty"):
        checks.warn("Bundle was built from a dirty working tree; release.py will refuse --publish.")

    wheels = sorted((release_dir / "dist").glob("*.whl"))
    sdists = sorted((release_dir / "dist").glob("*.tar.gz"))
    checks.check(
        "artifact_layout",
        lambda: 1 if wheels and sdists else (_raise("Expected one wheel and one sdist")),
    )
    if wheels:
        checks.check("wheel_contents", lambda: verify_wheel(wheels[0], version))
    if sdists:
        checks.check("sdist_contents", lambda: verify_sdist(sdists[0]))
    skill = release_dir / "skill" / "paknow" / "SKILL.md"
    checks.check("skill_contract", lambda: verify_skill(skill))
    checks.check(
        "no_absolute_paths",
        lambda: _scan_for_absolute_paths(release_dir),
    )

    try:
        report = verify_installed(release_dir, workdir, checks)
    except Exception as exc:  # noqa: BLE001
        checks.failures += 1
        checks.items.append(
            {"name": "installed_probe", "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        )
        report = {}

    _finish(checks, workdir, report)


def _raise(message: str):
    raise RuntimeError(message)


def _scan_for_absolute_paths(release_dir: Path) -> int:
    patterns = [re.compile(r"[A-Za-z]:\\\\"), re.compile(r"/home/[^\s]+")]
    scanned = 0
    for path in release_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".md", ".py", ".toml", ".json"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in patterns:
            if pattern.search(text):
                raise RuntimeError(f"Absolute path found in {path}: {pattern.pattern}")
        scanned += 1
    return scanned


def _finish(checks: Checks, workdir: Path, probe_report: dict) -> None:
    report = {
        "ok": checks.failures == 0,
        "failures": checks.failures,
        "warnings": checks.warnings,
        "checks": checks.items,
        "probe": probe_report,
        "workdir": str(workdir),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    (workdir / "VERIFICATION.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
