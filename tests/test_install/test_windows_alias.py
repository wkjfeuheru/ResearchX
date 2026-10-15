"""Installer regressions for Windows command aliases."""

from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib


def test_pyproject_exposes_openh_console_script():
    data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    assert scripts["openh"] == "researchx.cli:app"
    assert scripts["oh"] == "researchx.cli:app"


def test_installed_console_aliases_resolve_to_the_same_cli():
    """The retired installer is replaced by wheel-generated console entry points."""
    from importlib.metadata import distribution
    from researchx.cli import app

    entry_points = {
        entry.name: entry
        for entry in distribution("researchx-ai").entry_points
        if entry.group == "console_scripts"
    }
    for name in ("rx", "oh", "openh"):
        assert name in entry_points
        assert entry_points[name].load() is app
    assert "on PowerShell use `openh web` or `oh.exe web`" in Path("README.md").read_text()


def test_module_launcher_is_a_portable_fallback_without_console_scripts(tmp_path):
    """Windows can bypass the built-in oh alias and missing PATH launchers."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "researchx", "--help"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "web" in result.stdout and "setup" in result.stdout
    assert "python -m researchx web" in Path("README.md").read_text()
