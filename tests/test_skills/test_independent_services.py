"""Independent MCP and Hooks remain available alongside standalone Skills."""

import sys
from pathlib import Path

from researchx.config.settings import Settings
from researchx.hooks.loader import load_hook_registry
from researchx.hooks import HookEvent
from researchx.mcp.client import McpClientManager
from researchx.mcp.config import load_mcp_server_configs
from researchx.tools import create_research_tool_registry
from researchx.tools.base import ToolExecutionContext


async def test_skill_and_independent_mcp_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCHX_CONFIG_DIR", str(tmp_path / "config"))
    skill = tmp_path / "config/skills/fixture"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: fixture\ndescription: Example\n---\nFixture skill content")
    script = Path(__file__).resolve().parents[1] / "fixtures/fake_mcp_server.py"
    settings = Settings(mcp_servers={"fixture": {"type": "stdio", "command": sys.executable, "args": [str(script)]}},
        hooks={"session_start": [{"type": "command", "command": "printf start", "priority": 7}]})
    assert load_hook_registry(settings).get(HookEvent.SESSION_START)[0].priority == 7
    manager = McpClientManager(load_mcp_server_configs(settings))
    await manager.connect_all()
    try:
        registry = create_research_tool_registry(manager)
        tool = registry.get("skill")
        context = ToolExecutionContext(cwd=tmp_path, settings=settings)
        result = await tool.execute(tool.parse_input({"name": "fixture"}), context)
        assert "Fixture skill content" in result.output
        remote = registry.get("mcp__fixture__hello")
        result = await remote.execute(remote.parse_input({"name": "skills"}), context)
        assert result.output == "fixture-hello:skills"
    finally:
        await manager.close()
