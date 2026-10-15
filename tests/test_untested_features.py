"""Comprehensive integration tests for all previously untested ResearchX features.

Run with: python -m pytest tests/test_untested_features.py -v --tb=short -x
Or standalone: python tests/test_untested_features.py

Retains shared hooks, skills, plugins, configuration and session storage checks.
"""

from __future__ import annotations


import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from researchx.config.settings import Settings


# ====================================================================
# Helpers
# ====================================================================


# ====================================================================
# 1. Hooks: command hook blocks a tool call
# ====================================================================
async def test_hooks_command_block():
    """Register a pre_tool_use command hook that blocks bash, verify it fires."""
    from researchx.hooks.events import HookEvent
    from researchx.hooks.loader import HookRegistry
    from researchx.hooks.schemas import CommandHookDefinition
    from researchx.hooks.executor import HookExecutor, HookExecutionContext

    registry = HookRegistry()
    # Hook: run 'echo BLOCKED' when bash is used — block_on_failure means if exit!=0 it blocks
    # We use a command that always exits 1 to simulate blocking
    hook = CommandHookDefinition(
        type="command",
        command="exit 1",
        matcher="bash",
        block_on_failure=True,
        timeout_seconds=5,
    )
    registry.register(HookEvent.PRE_TOOL_USE, hook)
    print(f"  Registered pre_tool_use hook: {hook}")

    api = None
    ctx = HookExecutionContext(cwd=Path.cwd(), api_client=api, default_model="test-model")
    executor = HookExecutor(registry, ctx)

    # Trigger with bash — should block
    result = await executor.execute(
        HookEvent.PRE_TOOL_USE,
        {"tool_name": "bash", "tool_input": {"command": "ls"}, "event": "pre_tool_use"},
    )
    print(f"  bash hook result: blocked={result.blocked}, reason={result.reason}")

    # Trigger with glob — should NOT block (matcher doesn't match)
    result2 = await executor.execute(
        HookEvent.PRE_TOOL_USE,
        {"tool_name": "glob", "tool_input": {"pattern": "*.py"}, "event": "pre_tool_use"},
    )
    print(f"  glob hook result: blocked={result2.blocked}")

    assert result.blocked and not result2.blocked


# ====================================================================
# 2. Hooks: post_tool_use hook runs after tool
# ====================================================================
async def test_hooks_post_tool_use():
    """Register a post_tool_use hook that logs tool output, verify it runs."""
    from researchx.hooks.events import HookEvent
    from researchx.hooks.loader import HookRegistry
    from researchx.hooks.schemas import CommandHookDefinition
    from researchx.hooks.executor import HookExecutor, HookExecutionContext

    registry = HookRegistry()
    hook = CommandHookDefinition(
        type="command",
        command="echo POST_HOOK_FIRED",
        timeout_seconds=5,
    )
    registry.register(HookEvent.POST_TOOL_USE, hook)

    api = None
    ctx = HookExecutionContext(cwd=Path.cwd(), api_client=api, default_model="test-model")
    executor = HookExecutor(registry, ctx)

    result = await executor.execute(
        HookEvent.POST_TOOL_USE,
        {"tool_name": "bash", "tool_output": "hello", "event": "post_tool_use"},
    )
    print(f"  post_tool_use results: {len(result.results)} hooks fired")
    print(f"  output: {result.results[0].output if result.results else 'none'}")
    any_fired = len(result.results) > 0 and result.results[0].success
    assert any_fired


# ====================================================================
# 3. Hooks integrated into agent loop — hook blocks a dangerous command
# ====================================================================


# ====================================================================
# 4. Skills: load from directory and list
# ====================================================================
async def test_skills_load():
    """Create skill files, load them, verify registry."""
    from researchx.skills.registry import SkillRegistry
    from researchx.skills.loader import load_user_skills

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create skill files
        commit_dir = Path(tmpdir) / "commit"
        commit_dir.mkdir()
        (commit_dir / "SKILL.md").write_text("""---
name: commit
description: Create a git commit with a good message
---
Read the git diff, then create a commit with a descriptive message.
""")
        review_dir = Path(tmpdir) / "review-pr"
        review_dir.mkdir()
        (review_dir / "SKILL.md").write_text("""---
name: review-pr
description: Review a pull request for issues
---
Fetch the PR diff, review for bugs, style issues, and security problems.
""")

        # Monkey-patch skills dir
        import researchx.skills.loader as sl

        orig = sl.get_user_skills_dir
        sl.get_user_skills_dir = lambda: Path(tmpdir)

        skills = load_user_skills()
        print(f"  Loaded {len(skills)} skills: {[s.name for s in skills]}")

        reg = SkillRegistry()
        for s in skills:
            reg.register(s)

        commit = reg.get("commit")
        review = reg.get("review-pr")
        print(f"  commit skill: {commit.description if commit else 'NOT FOUND'}")
        print(f"  review-pr skill: {review.description if review else 'NOT FOUND'}")
        print(f"  All skills: {[s.name for s in reg.list_skills()]}")

        sl.get_user_skills_dir = orig

        assert (
            commit is not None
            and review is not None
            and "commit" in commit.load_content().lower()
            and {"commit", "review-pr"}.issubset({item.name for item in reg.list_skills()})
        )


# ====================================================================
# 5. Plugins: load manifest and discover skills
# ====================================================================
async def test_standalone_skills_load():
    from researchx.skills.loader import load_skills_from_dirs

    with tempfile.TemporaryDirectory() as tmpdir:
        skill = Path(tmpdir) / "deploy"
        skill.mkdir()
        (skill / "SKILL.md").write_text("---\nname: deploy\ndescription: Deploy application\n---\nBuild and deploy")
        loaded = load_skills_from_dirs([Path(tmpdir)])
        assert len(loaded) == 1 and loaded[0].name == "deploy"


# ====================================================================
# 6. Memory: add, list, search, remove
# ====================================================================


# ====================================================================
# 7. Session storage: save, list, load, export markdown
# ====================================================================
async def test_session_storage():
    """Test session save/load/list/export cycle."""
    from researchx.services.sessions.storage import (
        save_session_snapshot,
        load_session_snapshot,
        list_session_snapshots,
        export_session_markdown,
    )
    from researchx.engine.messages import ConversationMessage, TextBlock
    from researchx.api.usage import UsageSnapshot

    with tempfile.TemporaryDirectory() as tmpdir:
        messages = [
            ConversationMessage.from_user_text("Hello, analyze this code"),
            ConversationMessage(
                role="assistant", content=[TextBlock(text="I'll read the file first.")]
            ),
            ConversationMessage.from_user_text("Thanks, now fix the bug"),
            ConversationMessage(
                role="assistant", content=[TextBlock(text="Fixed the null check at line 42.")]
            ),
        ]
        usage = UsageSnapshot(input_tokens=500, output_tokens=200)

        # Save
        path = (await save_session_snapshot(
            cwd=tmpdir,
            model="test-model",
            system_prompt="Test prompt",
            messages=messages,
            usage=usage,
            session_id="test-session-123",
        ))
        print(f"  Saved to: {path}")

        # List
        snapshots = (await list_session_snapshots(tmpdir))
        print(f"  Listed: {len(snapshots)} snapshots")

        # Load latest
        loaded = (await load_session_snapshot(tmpdir))
        print(f"  Loaded: model={loaded.get('model')}, messages={len(loaded.get('messages', []))}")

        # Load by ID

        # Export markdown
        md_path = export_session_markdown(cwd=tmpdir, messages=messages)
        md_content = md_path.read_text() if md_path.exists() else ""
        print(f"  Exported markdown: {len(md_content)} chars")

        assert (
            any(item["session_id"] == path.name for item in snapshots)
            and len(snapshots) >= 1
            and loaded is not None
            and loaded.get("model") == "test-model"
            and len(md_content) > 0
        )


# ====================================================================
# 8. Config: load settings, merge overrides, path functions
# ====================================================================
async def test_config_settings():
    """Test settings loading, env var overrides, and path functions."""
    from researchx.config.settings import load_settings
    from researchx.config.paths import (
        get_config_dir,
        get_sessions_dir,
    )

    # Default settings
    s = Settings()
    print(f"  Default model: {s.model}")
    print(f"  Default permission mode: {s.permission.mode}")
    print(f"  Default memory enabled: {s.research_memory.enabled}")

    # Merge overrides
    s2 = s.merge_cli_overrides(model="kimi-k2.5", verbose=True)
    print(f"  After override: model={s2.model}, verbose={s2.verbose}")

    # With custom settings file
    with tempfile.TemporaryDirectory() as tmpdir:
        config_file = Path(tmpdir) / "settings.json"
        config_file.write_text(
            json.dumps(
                {
                    "model": "custom-model",
                    "permission": {"mode": "plan"},
                    "memory": {"enabled": False, "max_files": 10},
                }
            )
        )
        loaded = load_settings(config_path=config_file)
        print(
            f"  Loaded from file: model={loaded.model}, perm={loaded.permission.mode}, memory={loaded.research_memory.enabled}"
        )

    # Path functions
    config_dir = get_config_dir()
    sessions_dir = get_sessions_dir()
    print(f"  Config dir: {config_dir}")
    print(f"  Sessions dir: {sessions_dir}")

    assert (
        s.model != ""
        and s2.model == "kimi-k2.5"
        and s2.verbose is True
        and loaded.model == "custom-model"
        and loaded.research_memory.enabled is True
        and config_dir.name == ".researchx"
    )


# ====================================================================
# 9. Commands: register and lookup slash commands
# ====================================================================


# ====================================================================
# 10. Web fetch: real URL fetch in agent loop
# ====================================================================


# ====================================================================
# 11. Worktree: real git worktree create/list/remove
# ====================================================================


# ====================================================================
# 12. MCP types: config models validate correctly
# ====================================================================
async def test_mcp_types():
    """Test MCP config model validation."""
    from researchx.mcp.types import McpStdioServerConfig, McpToolInfo, McpConnectionStatus

    # Stdio config
    stdio = McpStdioServerConfig(
        command="npx", args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    )
    print(f"  Stdio config: cmd={stdio.command}, args={stdio.args}")

    # Tool info
    tool = McpToolInfo(
        server_name="filesystem",
        name="read_file",
        description="Read a file",
        input_schema={"type": "object"},
    )
    print(f"  Tool: {tool.server_name}/{tool.name}")

    # Connection status
    status = McpConnectionStatus(name="filesystem", state="connected", tools=[tool])
    print(f"  Status: {status.name}={status.state}, tools={len(status.tools)}")

    assert stdio.command == "npx" and tool.name == "read_file" and status.state == "connected"


# ====================================================================
# 13. Config paths: all path functions return valid paths
# ====================================================================
async def test_config_paths(monkeypatch, tmp_path):
    """Verify all config path functions return sensible paths."""
    from researchx.config.paths import (
        get_config_dir,
        get_config_file_path,
        get_data_dir,
        get_logs_dir,
        get_sessions_dir,
    )

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    for name in ("RESEARCHX_DATA_DIR", "RESEARCHX_CONFIG_DIR", "RESEARCHX_LOGS_DIR"):
        monkeypatch.delenv(name, raising=False)
    paths = {
        "config_dir": get_config_dir(),
        "config_file": get_config_file_path(),
        "data_dir": get_data_dir(),
        "logs_dir": get_logs_dir(),
        "sessions_dir": get_sessions_dir(),
    }
    for name, p in paths.items():
        print(f"  {name}: {p}")

    # This case verifies defaults, independently of the test database content root.
    all_under_home = all(".researchx" in str(p) for p in paths.values())
    assert all_under_home


# ====================================================================
# 14. Combined: hooks + skills + agent loop on AutoAgent
# ====================================================================


# ====================================================================
# 15. Multi-agent + worktree + team: full swarm on AutoAgent
# ====================================================================


# ====================================================================
# Main runner
# ====================================================================
