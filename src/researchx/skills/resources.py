"""Skill resource boundaries shared by entrypoints, navigation and file reads."""

from __future__ import annotations

from typing import TYPE_CHECKING
from pathlib import Path
import os

if TYPE_CHECKING:
    from researchx.tools.base import ToolExecutionContext

RESOURCE_DIRS = ("references", "scripts", "templates", "assets")
IGNORED_PARTS = {"__pycache__", "tests", ".pytest_cache", ".cache", ".git"}


def resolve_resource(base: str | Path, candidate: str | Path) -> Path:
    """Reject traversal and symlinks escaping the Skill root."""
    root = Path(os.path.abspath(Path(base).expanduser()))
    if root.resolve() != root:
        raise ValueError("Skill resource root was replaced by a symlink")
    path = Path(candidate)
    if ".." in path.parts:
        raise ValueError("Skill resource path traversal is forbidden")
    path = path if path.is_absolute() else root / path
    resolved = path.resolve()
    if not path.is_relative_to(root) or not resolved.is_relative_to(root):
        raise ValueError("Skill resource escapes its root")
    return resolved


def runtime_resources(entrypoint: Path) -> list[Path]:
    """Enumerate safe runtime files without reading them or following directory links."""
    base = entrypoint.parent.resolve()
    paths = [resolve_resource(base, entrypoint)]
    if entrypoint.name == "SKILL.md":
        for directory in RESOURCE_DIRS:
            root = base / directory
            if not root.is_dir():
                continue
            try:
                resolve_resource(base, root)
            except ValueError:
                continue
            for current, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = sorted(
                    d
                    for d in dirs
                    if d not in IGNORED_PARTS and not (Path(current) / d).is_symlink()
                )
                for name in sorted(files):
                    path = Path(current) / name
                    if path.suffix in {".pyc", ".pyo"} or name.startswith(".skill-index"):
                        continue
                    try:
                        resolve_resource(base, path)
                    except ValueError:
                        continue
                    if path.is_file():
                        paths.append(path)
    return sorted(paths, key=lambda path: path.relative_to(base).as_posix())


def validate_skill_file_path(candidate: Path) -> None:
    """Apply the Skill boundary before a generic file tool resolves symlinks."""
    for parent in candidate.parents:
        if (parent / "SKILL.md").is_file():
            resolve_resource(parent, candidate)
            return


def selected_skill_resource(context: ToolExecutionContext, candidate: Path) -> Path | None:
    """Allow read-only access only to explicitly selected, still-enabled Skill roots."""
    roots = context.metadata.get("selected_skill_roots", {})
    if not roots:
        roots = getattr(context.metadata.get("query_context"), "tool_metadata", {}).get(
            "selected_skill_roots", {}
        )
    for name, base in roots.items():
        if not candidate.is_relative_to(Path(base)):
            continue
        from researchx.skills.loader import load_skill_registry

        registry = load_skill_registry(
            context.cwd,
            settings=context.metadata.get("skill_settings") or context.settings,
            extra_skill_dirs=context.metadata.get("extra_skill_dirs"),
        )
        skill = registry.get(name)
        if not skill or skill.base_dir != base or skill.metadata.status in {"draft", "retired"}:
            raise ValueError("Skill resource belongs to a disabled or unpublished Skill")
        return resolve_resource(base, candidate)
    return None
