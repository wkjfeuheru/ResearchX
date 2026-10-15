"""Legacy migration retains switches, credentials, files and discovery safety."""

import json
from pathlib import Path

import pytest

from researchx.config.settings import Settings, load_settings
from researchx.skills.loader import load_skill_registry
from researchx.skills.migration import migrate_legacy_settings
from researchx.web.catalog import skills_list, skill_detail, toggle_skill


@pytest.mark.parametrize("package,names", [
    ("analysis-modeling", ["financial-statement-analysis", "company-event-monitor", "research-report-digest", "earnings-forecast"]),
    ("report-generation", ["deep-investment-report", "financial-commentary", "industry-commentary", "industry-deep-dive"]),
])
def test_legacy_package_switch_wins_and_migration_is_idempotent(tmp_path, package, names):
    path = tmp_path / "settings.json"
    original = {"enabled_plugins": {package: False}, "enabled_skills": {names[0]: True}, "api_key": "preserve", "unrelated": {"future": 1}}
    path.write_text(json.dumps(original))
    migrate_legacy_settings(path)
    after = json.loads(path.read_text())
    assert all(after["enabled_skills"][name] is False for name in names)
    assert after["api_key"] == original["api_key"] and after["unrelated"] == original["unrelated"]
    assert json.loads(path.with_suffix(".json.before-skills").read_text()) == original
    before = path.read_bytes()
    migrate_legacy_settings(path)
    assert path.read_bytes() == before


def test_individual_explicit_switch_precedes_legacy_business_switch(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"enabled_plugins": {"deep-investment-report": False}, "enabled_skills": {"deep-investment-report": True}}))
    migrate_legacy_settings(path)
    assert load_settings(path).enabled_skills["deep-investment-report"] is True


def test_unknown_legacy_settings_are_diagnosed_and_backed_up(tmp_path, caplog):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"enabled_plugins": {"unknown": False}}))
    migrate_legacy_settings(path)
    assert "manual migration" in caplog.text
    assert json.loads(path.with_suffix(".json.before-skills").read_text())["enabled_plugins"] == {"unknown": False}


def test_catalog_lists_sources_disabled_skills_and_never_imports_scripts(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCHX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    marker = tmp_path / "imported"
    for base, name in [(tmp_path / "config/skills", "user-example"), (tmp_path / ".researchx/skills", "project-example")]:
        skill = base / name
        (skill / "scripts").mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Example\n---\nWorkflow")
        (skill / "scripts/__init__.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    cards = skills_list(str(tmp_path))
    assert {s["source"] for s in cards} == {"builtin", "user", "project"}
    item = next(s for s in cards if s["name"] == "user-example")
    toggle_skill(str(tmp_path), item["id"], False)
    assert not next(s for s in skills_list(str(tmp_path)) if s["id"] == item["id"])["enabled"]
    assert "Workflow" in skill_detail(str(tmp_path), item["id"])["content"]
    assert load_skill_registry(tmp_path).get("user-example") is None
    assert not marker.exists()


def test_unknown_extension_files_are_retained_without_execution(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("RESEARCHX_CONFIG_DIR", str(tmp_path / "config"))
    root = tmp_path / "config/plugins/unknown"
    (root / "tools").mkdir(parents=True)
    marker = tmp_path / "imported"
    tool = root / "tools/echo.py"
    tool.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    (root / "plugin.json").write_text('{"name":"unknown"}')
    load_skill_registry(tmp_path, settings=Settings())
    assert "manual migration" in caplog.text
    assert tool.exists() and not marker.exists()


def test_known_installed_package_preserves_scripts_and_disabled_default(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCHX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    root = tmp_path / "config/plugins/analysis-modeling"
    source = root / "skills/earnings-forecast"
    (source / "scripts").mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: earnings-forecast\ndescription: Custom forecasting\n---\nWorkflow")
    script = source / "scripts/custom.py"
    script.write_text("custom_algorithm_version = 7\n")
    (root / "plugin.json").write_text(json.dumps({"name":"analysis-modeling", "enabled_by_default":False}))
    registry = load_skill_registry(tmp_path, settings=Settings(), include_disabled=True)
    skill = registry.get("earnings-forecast")
    assert skill.source == "user" and skill.enabled is False
    assert (Path(skill.base_dir) / "scripts/custom.py").read_text() == script.read_text()
    assert script.exists()
    assert load_skill_registry(tmp_path, settings=Settings()).get("earnings-forecast") is None
    assert skill.id in load_settings().enabled_skills


def test_legacy_boolean_coercion_is_preserved(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"enabled_plugins":{"analysis-modeling":"false"}, "enabled_skills":{"earnings-forecast":"true"}}))
    migrate_legacy_settings(path)
    assert load_settings(path).enabled_skills["earnings-forecast"] is False


def test_legacy_manifest_cannot_escape_package(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("RESEARCHX_CONFIG_DIR", str(tmp_path / "config"))
    root = tmp_path / "config/plugins/analysis-modeling"
    source = root / "skills/earnings-forecast"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: earnings-forecast\ndescription: Test\n---\n")
    external = tmp_path / "outside.json"
    external.write_text('{"name":"analysis-modeling"}')
    (root / "plugin.json").symlink_to(external)
    load_skill_registry(tmp_path, settings=Settings())
    assert not (tmp_path / "config/skills/earnings-forecast").exists()
    assert "manual migration" in caplog.text
    assert external.exists()


def test_legacy_skill_directory_cannot_escape_package(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("RESEARCHX_CONFIG_DIR", str(tmp_path / "config"))
    root = tmp_path / "config/plugins/analysis-modeling"
    root.mkdir(parents=True)
    external = tmp_path / "outside/earnings-forecast"
    external.mkdir(parents=True)
    (external / "SKILL.md").write_text("---\nname: earnings-forecast\ndescription: Outside\n---\n")
    (root / "plugin.json").write_text('{"name":"analysis-modeling"}')
    (root / "skills").symlink_to(external.parent, target_is_directory=True)
    load_skill_registry(tmp_path, settings=Settings())
    assert not (tmp_path / "config/skills/earnings-forecast").exists()
    assert "manual migration" in caplog.text
    assert (external / "SKILL.md").exists()
