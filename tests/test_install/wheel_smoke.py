"""Opt-in installed-wheel smoke; run with the clean environment's Python."""

import asyncio
import importlib
import json
import pkgutil
import tempfile
import os
import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path
import httpx
from researchx.tools import create_research_tool_registry
from researchx.tools.base import ToolExecutionContext
from researchx.tools.bash_tool import BashTool, BashToolInput
from researchx.state.store import ResearchStore
from researchx.skills import load_skill_registry
from researchx.web.app import create_app


async def main():
    from researchx.storage.database import database_lifespan

    async with database_lifespan():
        await installed_smoke()


async def installed_smoke():
    import researchx

    modules = list(pkgutil.walk_packages(researchx.__path__, "researchx."))
    for module in modules:
        importlib.import_module(module.name)
    dist = distribution("researchx-ai")
    names = [str(f) for f in dist.files]
    assert not any(
        "/ui/" in f or "/channels/" in f or "ohmo/" in f or "/_frontend/" in f or "/memory/" in f
        for f in names
    )
    assert any("/_web/index.html" in f for f in names)
    assert not any("skill-authoring/" in name or "skill-creator/" in name for name in names)
    research_skills = {
        "financial-statement-analysis": ("financial", "analyze_statements"),
        "company-event-monitor": ("monitor", "normalize_events"),
        "deep-investment-report": ("deep", "forecast"),
        "research-report-digest": ("digest", "digest_reports"),
    }
    for plugin, (_, script) in research_skills.items():
        base = "researchx/skills/bundled/"
        for resource in (
            "SKILL.md",
            f"scripts/{script}.py",
            "scripts/models.py",
            "scripts/export_report.py",
            "scripts/__init__.py",
            "templates/input.schema.json",
            "templates/report.md",
        ):
            assert base + f"{plugin}/" + resource in names
        assert base + f"{plugin}/scripts/run.py" not in names
    assert not any("/utils/" in name for name in names)
    assert not any("/tools/research/" in name for name in names)
    tool_modules = [
        name for name in names if name.startswith("researchx/tools/") and name.endswith(".py")
    ]
    assert all(
        len(Path(name).parts) == 3
        and (
            Path(name).name in {"__init__.py", "base.py", "contracts.py"}
            or name.endswith("_tool.py")
        )
        for name in tool_modules
    )
    assert "researchx/workspace/session_files.py" in names
    assert "researchx/workspace/documents.py" in names
    assert not any("sample_plugins" in f for f in names)
    assert not any(f.startswith("researchx/plugins/") for f in names)
    assert {e.name for e in dist.entry_points} == {"oh", "openh", "rx"}
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        os.environ["RESEARCHX_CONFIG_DIR"] = str(cwd / "config")
        os.environ["RESEARCHX_DATA_DIR"] = str(cwd / "data")
        assert load_skill_registry(cwd).get("skill-creator") is None
        fixtures = Path(__file__).parents[1] / "fixtures" / "research_skills"
        for plugin, (kind, script) in research_skills.items():
            skill = load_skill_registry(cwd).get(plugin)
            assert skill is not None and skill.metadata.status == "active"
            assert len(skill.metadata.content_hash) == 64, (
                "wheel build index should supply a cold L0 hash"
            )
            from researchx.skills.metadata import content_hash

            assert len(content_hash(Path(skill.path))) == 64
            assert len(load_skill_registry(cwd).get(plugin).metadata.content_hash) == 64
            scripts = Path(skill.base_dir) / "scripts"
            computed = cwd / kind / "computed.json"
            commands = [
                [sys.executable, str(scripts / f"{script}.py"), "--schema"],
                [
                    sys.executable,
                    str(scripts / f"{script}.py"),
                    "--input",
                    str(fixtures / f"{kind}.json"),
                    "--output",
                    str(computed),
                ],
                [
                    sys.executable,
                    str(scripts / "export_report.py"),
                    "--input",
                    str(computed),
                    "--output-dir",
                    str(cwd / kind),
                    "--session-dir",
                    str(cwd / "skill-session"),
                    "--task-id",
                    "installed-script",
                ],
            ]
            for command in commands:
                executed = subprocess.run(
                    command,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env={**os.environ, "RESEARCHX_ISOLATED_EXPORT": "1"},
                )
                assert executed.returncode == 0, executed.stderr
                response = json.loads(executed.stdout)
            assert response["status"] == "complete" and len(response["files"]) == 4
            assert response["artifacts"] == []
            exporter = importlib.import_module(
                f"researchx.skills.bundled.{plugin}.scripts.export_report"
            )
            result_module = importlib.import_module(
                f"researchx.skills.bundled.{plugin}.scripts.models"
            )
            result_type = getattr(
                result_module,
                {
                    "financial": "FinancialResult",
                    "monitor": "MonitorResult",
                    "digest": "DigestResult",
                    "deep": "DeepResult",
                }[kind],
            )
            from researchx.workspace.exports import export_registered
            from researchx.state.models import SourceUsage

            registered_store = ResearchStore(cwd, "a" * 12)
            fixture_payload = json.loads(computed.read_text())

            async def bind_fixture_sources(value):
                if isinstance(value, dict):
                    if "locator" in value and "title" in value and not value.get("source_id"):
                        source = await registered_store.capture(
                            origin_id=f"wheel-fixture:{value['locator']}", kind="web",
                            locator=value["locator"], title=value["title"], content="公开离线安装测试资料",
                            usage=SourceUsage(category="public", provider="wheel_fixture",
                                licence_status="pending", model_use_allowed=True,
                                internal_export_allowed=True, completeness="full"),
                        )
                        value["source_id"] = source.id
                    for item in value.values():
                        await bind_fixture_sources(item)
                elif isinstance(value, list):
                    for item in value:
                        await bind_fixture_sources(item)

            await bind_fixture_sources(fixture_payload)

            registered = await export_registered(
                result_type.model_validate(fixture_payload),
                cwd / kind,
                registered_store,
                task_id="installed-script",
                render_markdown=exporter.render_markdown,
                sheets=exporter.SHEETS,
            )
            assert len(registered["artifacts"]) == 4
        for name in (
            "earnings-forecast",
            "financial-commentary",
            "industry-commentary",
            "industry-deep-dive",
        ):
            skill = load_skill_registry(cwd).get(name)
            assert skill and skill.content is None and len(skill.metadata.content_hash) == 64
            base = Path(skill.base_dir)
            script = "forecast" if name == "earnings-forecast" else "validate_report"
            input_path = (
                fixtures / "deep.json"
                if name == "earnings-forecast"
                else base / "templates/input.json"
            )
            output = cwd / name / "computed.json"
            for command in (
                [sys.executable, str(base / f"scripts/{script}.py"), "--schema"],
                [
                    sys.executable,
                    str(base / f"scripts/{script}.py"),
                    "--input",
                    str(input_path),
                    "--output",
                    str(output),
                ],
                [
                    sys.executable,
                    str(base / "scripts/export_report.py"),
                    "--input",
                    str(output),
                    "--output-dir",
                    str(cwd / name),
                ],
            ):
                executed = subprocess.run(
                    command,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env={**os.environ, "RESEARCHX_ISOLATED_EXPORT": "1"},
                )
                assert executed.returncode == 0, executed.stderr
        parsed = subprocess.run(
            [
                sys.executable,
                "-m",
                "researchx.workspace.documents",
                "--input",
                str(fixtures / "annual-report.pdf"),
                "--output-dir",
                str(cwd / "parsed"),
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert parsed.returncode == 0, parsed.stderr
        store = ResearchStore(cwd, "a" * 12)
        source = await store.capture(origin_id="user", kind="user", content="wheel smoke")
        assert (await store.read_source(source)) == "wheel smoke"
        assert "research_memory" in {t.name for t in create_research_tool_registry().list_tools()}
        result = await BashTool().execute(
            BashToolInput(command="printf WHEEL_SHELL_OK"), ToolExecutionContext(cwd=cwd)
        )
        assert not result.is_error and result.output == "WHEEL_SHELL_OK"
        app = create_app(cwd=cwd)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as client,
        ):
            assert (await client.get("/api/health")).status_code == 200
            assert (await client.get("/")).status_code == 200
            item = await client.post(
                "/api/sessions",
                json={"profile_id": "claude-api"},
                headers={"origin": "http://localhost"},
            )
            assert item.status_code == 201, item.text
    print(
        f"PASS: {len(modules)} installed modules imported; no retired assets; Web/CLI/Shell/research persistence verified"
    )


if __name__ == "__main__":
    asyncio.run(main())


def test_built_wheel_includes_both_packages_and_runs_installed_smoke(tmp_path):
    """Build and execute from an isolated installation, rather than an editable checkout."""
    import zipfile

    root = Path(__file__).resolve().parents[2]
    built = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path / "dist")],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert built.returncode == 0, built.stderr
    wheel = next((tmp_path / "dist").glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
    assert not any("skill-authoring/" in name or "skill-creator/" in name for name in names)
    for package, skills in {
        "analysis-modeling": [
            "financial-statement-analysis",
            "company-event-monitor",
            "research-report-digest",
            "earnings-forecast",
        ],
        "report-generation": [
            "deep-investment-report",
            "financial-commentary",
            "industry-commentary",
            "industry-deep-dive",
        ],
    }.items():
        base = "researchx/skills/bundled/"
        for skill in skills:
            assert base + f"{skill}/SKILL.md" in names
            assert base + f"{skill}/templates/input.schema.json" in names
            assert base + f"{skill}/templates/report.md" in names
    env_dir = tmp_path / "installed"
    created = subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(env_dir)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert created.returncode == 0, created.stderr
    interpreter = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    installed = subprocess.run(
        ["uv", "pip", "install", "--python", str(interpreter), f"{wheel}[web]"],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert installed.returncode == 0, installed.stderr
    env = {
        **os.environ,
        "RESEARCHX_CONFIG_DIR": str(tmp_path / "config"),
        "RESEARCHX_DATA_DIR": str(tmp_path / "data"),
    }
    env.pop("PYTHONPATH", None)
    executed = subprocess.run(
        [str(interpreter), str(Path(__file__).resolve())],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert executed.returncode == 0, executed.stderr
    assert "PASS:" in executed.stdout
