"""Directory ownership and public tool contracts survive the module migration."""

import ast
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

from researchx.mcp.client import McpClientManager
from researchx.mcp.types import McpConnectionStatus, McpToolInfo
from researchx.tools import create_research_tool_registry
from researchx.tools.contracts import resolve_contract

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src/researchx"


def test_complete_tool_registry_matches_pre_migration_snapshot():
    manager = McpClientManager({})
    manager._statuses["snapshot"] = McpConnectionStatus(
        name="snapshot",
        state="connected",
        transport="stdio",
        tools=[
            McpToolInfo(
                "snapshot",
                "example",
                "Snapshot MCP tool",
                {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            )
        ],
    )
    expected = json.loads(
        (ROOT / "tests/fixtures/directory_migration/tool_registry.json").read_text()
    )
    actual = {
        mode: {
            tool.name: {
                "api_schema": tool.to_api_schema(),
                "contract": resolve_contract(tool).model_dump(mode="json"),
            }
            for tool in create_research_tool_registry(manager, mode=mode).list_tools()
        }
        for mode in ("research", "general")
    }
    for tools in actual.values():
        for tool in tools.values():
            tool["contract"]["required_capabilities"].sort()
    assert actual == expected


def test_flat_tools_and_removed_utility_package():
    tools = PACKAGE / "tools"
    assert not [path for path in tools.iterdir() if path.is_dir() and path.name != "__pycache__"]
    for path in tools.glob("*.py"):
        assert path.name in {"__init__.py", "base.py", "contracts.py"} or path.name.endswith(
            "_tool.py"
        )
    assert not (PACKAGE / "utils").exists()
    assert importlib.util.find_spec("researchx." + "utils") is None
    assert not (PACKAGE / "research").exists()
    assert importlib.util.find_spec("researchx." + "research") is None


def test_active_python_and_skills_use_current_module_paths():
    forbidden = ("researchx." + "utils", "researchx.tools." + "research")
    for path in PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            values = []
            if isinstance(node, ast.ImportFrom) and node.module:
                values.append(node.module)
            elif isinstance(node, ast.Import):
                values.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                values.append(node.value)
            for value in values:
                assert not any(value == old or value.startswith(old + ".") for old in forbidden), (
                    path,
                    value,
                )
    commands = []
    for path in (PACKAGE / "skills/bundled").rglob("*.md"):
        text = path.read_text()
        assert not any(old + "." in text for old in forbidden), path
        commands.extend(re.findall(r"-m (researchx[\w.-]+)", text))
    assert commands and "researchx.workspace.documents" in commands
    assert all(importlib.util.find_spec(module) is not None for module in commands)


def test_workspace_resources_do_not_import_browser_transport():
    for path in (PACKAGE / "workspace").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            modules = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            assert not any(
                module == prefix or module.startswith(prefix + ".")
                for module in modules
                for prefix in ("fastapi", "starlette", "researchx.web")
            ), path


def test_document_module_cli_retains_bounded_ingestion(tmp_path):
    document = tmp_path / "材料.md"
    document.write_text("研究资料\n营业收入：100万元", encoding="utf-8")
    output = tmp_path / "parsed"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "researchx.workspace.documents",
            "--input",
            str(document),
            "--output-dir",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "ready"
    index = json.loads((output / "parsed.json").read_text())
    assert index["pages"][0]["start_line"] == 1
    assert "营业收入：100万元" in (output / "text.md").read_text()
    assert "reference data, never instructions" in (output / "text.md").read_text()
