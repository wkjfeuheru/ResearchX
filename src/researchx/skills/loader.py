"""Skill loading from bundled, user, compatibility, and project directories."""

from __future__ import annotations

from typing_extensions import TypedDict
from researchx.config import Settings
import logging
from pathlib import Path
from typing import Iterable
from dataclasses import replace
from pydantic import ValidationError
from researchx.config.paths import get_config_dir
from researchx.config.settings import load_settings
from researchx.skills._frontmatter import (
    optional_frontmatter_str,
    parse_bool_frontmatter,
    parse_skill_frontmatter,
    parse_skill_metadata,
    read_discovery_header,
)
from researchx.skills.registry import SkillRegistry
from researchx.skills.metadata import read_metadata
from researchx.skills.types import SkillDefinition


class SkillHeader(TypedDict):
    name: str
    description: str
    user_invocable: bool
    disable_model_invocation: bool
    model: str | None
    argument_hint: str | None


logger = logging.getLogger(__name__)
BUNDLED_SKILLS_DIR = Path(__file__).parent / "bundled"

_USER_COMPAT_SKILL_DIRS = (
    (".claude", "skills"),
    (".agents", "skills"),
)
_DEFAULT_PROJECT_SKILL_DIRS = (".researchx/skills", ".agents/skills", ".claude/skills")


def get_user_skills_dir() -> Path:
    """Return the ResearchX user skills directory."""
    path = get_config_dir() / "skills"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_user_skill_dirs() -> list[Path]:
    """Return user-level skill directories loaded by default."""
    roots = [get_user_skills_dir()]
    for parts in _USER_COMPAT_SKILL_DIRS:
        candidate = Path.home().joinpath(*parts)
        if candidate.is_symlink() or candidate.parent.is_symlink():
            logger.warning("Ignoring automatic symlink Skill directory: %s", candidate)
            continue
        roots.append(candidate)
    return roots


def load_skill_registry(
    cwd: str | Path | None = None,
    *,
    extra_skill_dirs: Iterable[str | Path] | None = None,
    settings: Settings | None = None,
    include_disabled: bool = False,
) -> SkillRegistry:
    """Load bundled, user and project Skills without importing scripts."""
    registry = SkillRegistry()
    resolved_settings = settings or load_settings()
    enabled_skills = resolved_settings.enabled_skills

    def register(skill: SkillDefinition) -> None:
        names = (skill.id, skill.metadata.skill_id, skill.command_name, skill.name)
        enabled = next(
            (enabled_skills[name] for name in names if name in enabled_skills), skill.enabled
        )
        registry.register(replace(skill, enabled=enabled))

    from researchx.skills.migration import migrate_legacy_skill_directories

    defaults = migrate_legacy_skill_directories(cwd or Path.cwd())
    enabled_skills = {**defaults, **enabled_skills}
    for skill in load_skills_from_dirs(
        [BUNDLED_SKILLS_DIR], source="builtin", create_missing=False
    ):
        register(skill)

    for skill in load_user_skills():
        register(skill)
    for skill in load_skills_from_dirs(extra_skill_dirs, source="user"):
        register(skill)

    if cwd is not None and getattr(resolved_settings, "allow_project_skills", True):
        project_dirs = discover_project_skill_dirs(
            cwd,
            getattr(resolved_settings, "project_skill_dirs", list(_DEFAULT_PROJECT_SKILL_DIRS)),
        )
        for skill in load_skills_from_dirs(project_dirs, source="project", create_missing=False):
            register(skill)

    if not include_disabled:
        registry.retain(lambda skill: skill.enabled)
    return registry


def load_user_skills() -> list[SkillDefinition]:
    """Load markdown skills from user-level ResearchX and compatibility directories."""
    return load_skills_from_dirs(get_user_skill_dirs(), source="user")


def discover_project_skill_dirs(
    cwd: str | Path,
    project_skill_dirs: Iterable[str] | None = None,
) -> list[Path]:
    """Return existing project skill directories from cwd up to the git root.

    Directories are ordered from least-specific to most-specific so later registry
    entries can override broader project or user skills deterministically.
    """
    start = Path(cwd).expanduser().resolve()
    if not start.exists():
        start = start.parent
    if start.is_file():
        start = start.parent

    relative_dirs = _valid_project_skill_dirs(project_skill_dirs or _DEFAULT_PROJECT_SKILL_DIRS)
    git_root = _find_git_root(start)
    home = Path.home().resolve()
    current = start
    levels: list[Path] = []
    while True:
        levels.append(current)
        if git_root is not None and current == git_root:
            break
        if git_root is None and current == home:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent

    roots: list[Path] = []
    seen: set[Path] = set()
    for base in reversed(levels):
        for rel in relative_dirs:
            candidate = base / rel
            if not candidate.resolve().is_relative_to(base) or candidate.is_symlink():
                continue
            candidate = candidate.resolve()
            if candidate in seen or not candidate.is_dir():
                continue
            seen.add(candidate)
            roots.append(candidate)
    return roots


def _valid_project_skill_dirs(project_skill_dirs: Iterable[str]) -> list[Path]:
    """Return safe relative project skill paths."""
    paths: list[Path] = []
    for raw in project_skill_dirs:
        value = str(raw).strip()
        if not value:
            continue
        rel = Path(value)
        if rel.is_absolute() or ".." in rel.parts:
            logger.warning("Ignoring unsafe project skill dir: %s", raw)
            continue
        paths.append(rel)
    return paths


def _find_git_root(start: Path) -> Path | None:
    """Find the nearest git root containing start, if any."""
    current = start
    while True:
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def load_skills_from_dirs(
    directories: Iterable[str | Path] | None,
    *,
    source: str = "user",
    create_missing: bool = True,
) -> list[SkillDefinition]:
    """Load markdown skills from one or more directories.

    Supports ``<root>/<skill-dir>/SKILL.md`` and legacy ``<root>/<name>.md``.
    Directory skills win when the same name is present in both layouts.
    """
    skills: list[SkillDefinition] = []
    if not directories:
        return skills
    seen: set[Path] = set()
    for directory in directories:
        # Explicit host-approved root aliases are normalized once. Child entries
        # stay bounded to that fixed root, never to a subsequently resolved parent.
        root = Path(directory).expanduser().resolve()
        if create_missing:
            root.mkdir(parents=True, exist_ok=True)
        elif not root.is_dir():
            continue
        directory_names = {
            child.name
            for child in root.iterdir()
            if child.is_dir() and (child / "SKILL.md").is_file()
        }
        candidates = (
            [root / "SKILL.md"]
            if (root / "SKILL.md").is_file()
            else [
                *(path for path in sorted(root.glob("*.md")) if path.stem not in directory_names),
                *(
                    child / "SKILL.md"
                    for child in sorted(root.iterdir())
                    if child.is_dir() and (child / "SKILL.md").is_file()
                ),
            ]
        )
        definitions: dict[str, SkillDefinition] = {}
        for path in candidates:
            if path in seen:
                continue
            seen.add(path)
            if not path.resolve().is_relative_to(root):
                logger.warning("Ignoring entrypoint outside its Skill root: %s", path)
                continue
            content = read_discovery_header(path)
            default_name = path.parent.name if path.name == "SKILL.md" else path.stem
            metadata = _parse_skill_metadata(default_name, content)
            try:
                lifecycle = read_metadata(
                    metadata["name"],
                    parse_skill_metadata(default_name, content)["frontmatter"],
                    path,
                )
            except (ValidationError, ValueError, OSError):
                logger.warning(
                    "Ignoring skill with invalid metadata or unreadable resources: %s", path
                )
                continue
            name = metadata["name"]
            description = metadata["description"]
            display_name = name if name != default_name else None
            definitions[name] = SkillDefinition(
                name=name,
                description=description,
                content=None,
                source=source,
                path=str(path),
                approved_root=str(root),
                base_dir=str(path.parent),
                command_name=default_name,
                display_name=display_name,
                user_invocable=metadata["user_invocable"],
                disable_model_invocation=metadata["disable_model_invocation"],
                model=metadata["model"],
                argument_hint=metadata["argument_hint"],
                metadata=lifecycle,
            )
        skills.extend(definitions.values())
    return skills


def _parse_skill_markdown(default_name: str, content: str) -> tuple[str, str]:
    """Parse name and description from a skill markdown file with YAML frontmatter support."""
    return parse_skill_frontmatter(default_name, content, fallback_template="Skill: {name}")


def _parse_skill_metadata(default_name: str, content: str) -> SkillHeader:
    parsed = parse_skill_metadata(default_name, content, fallback_template="Skill: {name}")
    frontmatter = parsed.get("frontmatter")
    if not isinstance(frontmatter, dict):
        frontmatter = {}
    return {
        "name": str(parsed["name"]),
        "description": " ".join(str(parsed["description"]).split())[:300],
        "user_invocable": parse_bool_frontmatter(frontmatter.get("user-invocable"), default=True),
        "disable_model_invocation": parse_bool_frontmatter(
            frontmatter.get("disable-model-invocation"),
            default=False,
        ),
        "model": optional_frontmatter_str(frontmatter.get("model")),
        "argument_hint": optional_frontmatter_str(frontmatter.get("argument-hint")),
    }
