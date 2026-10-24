"""Fail locally, not five minutes into a CI run, when the trimmed install list
in ``.github/workflows/check.yml`` falls behind the imports the test suite
reaches at module scope.

``check.yml`` cannot install the full dependency set (torch, transformers,
sentence-transformers), so it carries a hand-picked subset. That subset drifted
from the code three times in one afternoon: a module-level ``import PIL`` made
ten test modules fail to collect. This walks the static import closure starting
at the test files and asserts every third-party name it reaches is installed.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "check.yml"
FIRST_PARTY = {"paknow", "apps", "tests", "scripts"}

# Import name -> distribution name, for the ones that disagree.
DISTRIBUTION = {
    "PIL": "pillow",
    "bs4": "beautifulsoup4",
    "yaml": "pyyaml",
    "fitz": "pymupdf",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "qdrant_client": "qdrant-client",
    "pydantic_settings": "pydantic-settings",
    "langchain_core": "langchain-core",
    "sentence_transformers": "sentence-transformers",
    "dateutil": "python-dateutil",
    "Crypto": "pycryptodome",
}


def _module_file(dotted: str) -> Path | None:
    base = ROOT.joinpath(*dotted.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _module_imports(path: Path) -> set[str]:
    """Absolute module names imported at module scope.

    Only ``tree.body`` is walked, so imports inside functions (lazy, optional)
    never count, and a ``try: ... except ImportError`` guard is skipped because
    it is a ``Try`` node, not an ``Import``.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def _closure() -> tuple[set[str], dict[str, Path]]:
    """Third-party roots reached from the test files, plus where each was seen."""
    seeds = sorted((ROOT / "tests").glob("**/*.py"))
    queue = [seed.relative_to(ROOT).with_suffix("").as_posix().replace("/", ".") for seed in seeds]
    seen: set[str] = set()
    reached: dict[str, Path] = {}
    third_party: set[str] = set()

    while queue:
        dotted = queue.pop()
        if dotted in seen:
            continue
        seen.add(dotted)
        path = _module_file(dotted)
        if path is None:
            continue
        for module in _module_imports(path):
            root = module.split(".")[0]
            if root in sys.stdlib_module_names:
                continue
            if root in FIRST_PARTY:
                queue.append(module)
                continue
            third_party.add(root)
            reached.setdefault(root, path)
    return third_party, reached


def _installed() -> set[str]:
    text = WORKFLOW.read_text(encoding="utf-8")
    specs = re.findall(r'"([^"]+)"', text)
    names = set()
    for spec in specs:
        if spec.startswith(">") or spec.startswith("<") or " " in spec:
            continue
        names.add(re.split(r"[\s\[<>=!~]", spec, maxsplit=1)[0].lower())
    return names


def test_test_closure_imports_are_installed_by_ci():
    third_party, reached = _closure()
    installed = _installed()

    missing = {
        name: (DISTRIBUTION.get(name, name.lower()), str(reached[name].relative_to(ROOT)))
        for name in third_party
        if DISTRIBUTION.get(name, name.lower()) not in installed
    }

    assert not missing, (
        "module-scope imports not installed by .github/workflows/check.yml "
        f"(import -> distribution, first file seen): {missing}"
    )


def test_workflow_list_is_not_empty():
    # Guards the parser: a regex that stops matching would make the test above vacuous.
    assert len(_installed()) >= 10
