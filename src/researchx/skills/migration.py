"""One-time migration of known packages and switches; retain original user files."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from pydantic import TypeAdapter

from researchx.config.paths import get_config_dir, get_config_file_path
from researchx.storage.filesystem import atomic_write_text
from researchx.storage.file_lock import exclusive_file_lock

log = logging.getLogger(__name__)
PACKAGES = {
    "analysis-modeling": (
        "financial-statement-analysis",
        "company-event-monitor",
        "research-report-digest",
        "earnings-forecast",
    ),
    "report-generation": (
        "deep-investment-report",
        "financial-commentary",
        "industry-commentary",
        "industry-deep-dive",
    ),
}
LEGACY_SKILLS = {
    "financial-statement-analysis",
    "company-event-monitor",
    "research-report-digest",
    "deep-investment-report",
}


def migrate_legacy_settings(path: Path) -> None:
    with exclusive_file_lock(path.with_suffix(path.suffix + ".lock")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if "enabled_plugins" not in raw and "allow_project_plugins" not in raw:
            return
        old = TypeAdapter(dict[str, bool]).validate_python(raw.get("enabled_plugins", {}))
        enabled = TypeAdapter(dict[str, bool]).validate_python(raw.get("enabled_skills", {}))
        unknown = set(old) - set(PACKAGES) - LEGACY_SKILLS
        if unknown:
            log.warning(
                "Unknown legacy extension switches require manual migration: %s", sorted(unknown)
            )
        for package, names in PACKAGES.items():
            for name in names:
                previous = enabled.get(name, old.get(name, True) if name in LEGACY_SKILLS else True)
                enabled[name] = bool(old.get(package, True) and previous)
        backup = path.with_suffix(path.suffix + ".before-skills")
        if not backup.exists():
            atomic_write_text(backup, path.read_text(encoding="utf-8"), mode=0o600)
        raw["enabled_skills"] = enabled
        raw.pop("enabled_plugins", None)
        raw.pop("allow_project_plugins", None)
        atomic_write_text(path, json.dumps(raw, ensure_ascii=False, indent=2) + "\n", mode=0o600)


def migrate_legacy_skill_directories(cwd: str | Path) -> dict[str, bool]:
    switches: dict[str, bool] = {}
    settings_path = get_config_file_path()
    backup = settings_path.with_suffix(settings_path.suffix + ".before-skills")
    old_settings = (
        json.loads(backup.read_text())
        if backup.exists()
        else json.loads(settings_path.read_text())
        if settings_path.exists()
        else {}
    )
    for base in (get_config_dir(), Path(cwd).resolve() / ".researchx"):
        old_root = base / "plugins"
        if not old_root.is_dir():
            continue
        marker = base / "skills" / ".legacy-migrated.json"
        with exclusive_file_lock(base / "skills" / ".migration.lock"):
            if marker.exists():
                switches.update(json.loads(marker.read_text()).get("switches", {}))
                continue
            migrated = []
            for package in sorted(old_root.iterdir()):
                if not package.is_dir():
                    continue
                if package.name not in PACKAGES or package.is_symlink():
                    log.warning(
                        "Unknown legacy extension retained for manual migration: %s", package.name
                    )
                    continue
                manifest_path = package / "plugin.json"
                if not manifest_path.is_file():
                    manifest_path = package / ".claude-plugin/plugin.json"
                try:
                    if not manifest_path.resolve().is_relative_to(package.resolve()):
                        raise ValueError("manifest outside package")
                    manifest = json.loads(manifest_path.read_text())
                    if manifest.get("name") != package.name:
                        raise ValueError("manifest mismatch")
                    manifest["enabled_by_default"] = TypeAdapter(bool).validate_python(
                        manifest.get("enabled_by_default", True)
                    )
                except (OSError, ValueError):
                    log.warning(
                        "Legacy package metadata requires manual migration: %s", package.name
                    )
                    continue
                project = base != get_config_dir()
                if project and not TypeAdapter(bool).validate_python(
                    old_settings.get("allow_project_plugins", False)
                ):
                    log.warning(
                        "Disabled project extension retained for manual migration: %s", package.name
                    )
                    continue
                for name in PACKAGES[package.name]:
                    source = package / "skills" / name
                    target = base / "skills" / name
                    if not (source / "SKILL.md").is_file():
                        continue
                    if (
                        not source.resolve().is_relative_to(package.resolve())
                        or source.is_symlink()
                        or any(p.is_symlink() for p in source.rglob("*"))
                    ):
                        log.warning(
                            "Legacy Skill with symlinks requires manual migration: %s", name
                        )
                        continue
                    if target.exists():
                        log.warning(
                            "Existing Skill retained; legacy copy requires manual migration: %s",
                            name,
                        )
                        continue
                    shutil.copytree(
                        source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
                    )
                    replacements = {
                        "researchx.plugins.research_script_support": "researchx.skills.runtime_support",
                        "researchx.plugins.bundled.report-generation.reporting": "researchx.skills._shared.reporting",
                        "researchx.plugins.bundled.report_generation.reporting": "researchx.skills._shared.reporting",
                        "researchx.plugins.bundled.analysis-modeling.skills.earnings-forecast.scripts": "researchx.skills._shared.forecasting",
                        "researchx.plugins.bundled.analysis_modeling.skills.earnings_forecast.scripts": "researchx.skills._shared.forecasting",
                    }
                    for script in (target / "scripts").rglob("*.py"):
                        text = script.read_text()
                        for previous, current in replacements.items():
                            text = text.replace(previous, current)
                        atomic_write_text(script, text)
                    from researchx.skills.types import SkillDefinition

                    skill_id = SkillDefinition(
                        name=name,
                        description="",
                        content=None,
                        source="project" if project else "user",
                        path=str(target / "SKILL.md"),
                    ).id
                    old_packages = TypeAdapter(dict[str, bool]).validate_python(
                        old_settings.get("enabled_plugins", {})
                    )
                    old_skills = TypeAdapter(dict[str, bool]).validate_python(
                        old_settings.get("enabled_skills", {})
                    )
                    package_enabled = old_packages.get(
                        package.name, manifest.get("enabled_by_default", True)
                    )
                    enabled = package_enabled and old_skills.get(
                        name, old_packages.get(name, True) if name in LEGACY_SKILLS else True
                    )
                    with exclusive_file_lock(
                        settings_path.with_suffix(settings_path.suffix + ".lock")
                    ):
                        current = (
                            json.loads(settings_path.read_text()) if settings_path.exists() else {}
                        )
                        original = settings_path.read_text() if settings_path.exists() else "{}"
                        if not backup.exists():
                            atomic_write_text(backup, original, mode=0o600)
                        current.setdefault("enabled_skills", {})[skill_id] = bool(enabled)
                        atomic_write_text(
                            settings_path,
                            json.dumps(current, ensure_ascii=False, indent=2) + "\n",
                            mode=0o600,
                        )
                    switches[skill_id] = bool(enabled)
                    migrated.append(name)
            atomic_write_text(
                marker,
                json.dumps({"version": 1, "skills": migrated, "switches": switches}) + "\n",
                mode=0o600,
            )

    return switches
