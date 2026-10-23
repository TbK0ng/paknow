from __future__ import annotations

from scripts import precommit_pytest


def test_missing_venv_returns_none(tmp_path):
    assert precommit_pytest.venv_python(tmp_path) is None


def test_windows_and_posix_interpreter_paths(tmp_path):
    windows = tmp_path / ".venv" / "Scripts" / "python.exe"
    windows.parent.mkdir(parents=True)
    windows.write_text("")
    assert precommit_pytest.venv_python(tmp_path) == windows

    posix_root = tmp_path / "posix"
    posix = posix_root / ".venv" / "bin" / "python"
    posix.parent.mkdir(parents=True)
    posix.write_text("")
    assert precommit_pytest.venv_python(posix_root) == posix
