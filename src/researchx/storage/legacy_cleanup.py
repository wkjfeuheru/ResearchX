"""Idempotent cleanup of registered derived files. User paths outside the root are kept."""

from __future__ import annotations

from pathlib import Path


def registered_derived_files(root: Path, relative_paths: list[str]) -> list[Path]:
    base = root.resolve()
    selected: list[Path] = []
    for relative in relative_paths:
        candidate = (base / relative).resolve()
        if candidate.is_relative_to(base) and candidate.is_file():
            selected.append(candidate)
    return selected


def cleanup_registered_files(root: Path, relative_paths: list[str]) -> list[str]:
    removed: list[str] = []
    for path in registered_derived_files(root, relative_paths):
        path.unlink()
        removed.append(str(path.relative_to(root.resolve())))
    return removed
