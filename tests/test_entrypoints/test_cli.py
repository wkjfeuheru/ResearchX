"""CLI smoke tests."""

import re
import sys
import types
from pathlib import Path

import pytest
from typer.testing import CliRunner

import researchx.cli as cli
from researchx.config import load_settings


app = cli.app


@pytest.fixture(autouse=True)
def isolated_keyring(monkeypatch):
    """Setup must not block on the developer keyring or its D-Bus prompt."""
    monkeypatch.setattr("researchx.auth.storage._keyring_available", lambda: False)


def test_cli_help():
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--help"],
        env={"NO_COLOR": "1", "COLUMNS": "160"},
    )
    plain_output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert result.exit_code == 0
    assert "投研工作台" in plain_output
    assert "setup" in plain_output
    assert "web" in plain_output
    assert "--print" not in plain_output
    assert "autopilot" not in plain_output


def test_setup_flow_selects_profile_and_model(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    monkeypatch.setenv("RESEARCHX_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("researchx.auth.storage._keyring_available", lambda: False)

    selected = []

    def fake_select(statuses, default_value=None):
        selected.append((tuple(statuses.keys()), default_value))
        return "codex"

    logged_in = []

    def fake_login(provider):
        logged_in.append(provider)

    monkeypatch.setattr("researchx.cli._select_setup_workflow", fake_select)
    monkeypatch.setattr("researchx.cli._prompt_model_for_profile", lambda profile: "gpt-5.4")
    monkeypatch.setattr("researchx.cli._login_provider", fake_login)

    result = runner.invoke(app, ["setup"])
    assert result.exit_code == 0
    assert "Setup complete:" in result.output
    assert logged_in == ["openai_codex"]

    settings = load_settings()
    assert settings.active_profile == "codex"
    assert settings.resolve_profile()[1].last_model == "gpt-5.4"


def test_select_from_menu_uses_questionary_when_tty(monkeypatch):
    answers = []

    class _Prompt:
        def ask(self):
            return "codex"

    fake_questionary = types.SimpleNamespace(
        Choice=lambda title, value, checked=False: {
            "title": title,
            "value": value,
            "checked": checked,
        },
        select=lambda title, choices, default=None: (
            answers.append((title, choices, default)) or _Prompt()
        ),
    )

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys, "__stdin__", sys.stdin)
    monkeypatch.setattr(cli.sys, "__stdout__", sys.stdout)
    monkeypatch.setitem(sys.modules, "questionary", fake_questionary)

    result = cli._select_from_menu(
        "Choose a provider workflow:",
        [("codex", "Codex"), ("claude-api", "Claude API")],
        default_value="codex",
    )

    assert result == "codex"
    assert answers


def test_setup_flow_existing_api_key_profile_can_update_secret(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    monkeypatch.setenv("RESEARCHX_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    from researchx.auth.manager import AuthManager
    from researchx.auth.storage import load_credential

    manager = AuthManager()
    manager.store_profile_credential("openai-compatible", "api_key", "old-key")

    selections = iter(["openai-compatible", "openai-compatible"])
    monkeypatch.setattr(
        "researchx.cli._select_setup_workflow", lambda *args, **kwargs: next(selections)
    )
    monkeypatch.setattr("researchx.cli._select_from_menu", lambda *args, **kwargs: next(selections))
    monkeypatch.setattr("researchx.cli._confirm_prompt", lambda *args, **kwargs: True)
    monkeypatch.setattr("researchx.auth.flows.ApiKeyFlow.run", lambda self: "new-key")
    monkeypatch.setattr("researchx.cli._prompt_model_for_profile", lambda profile: "gpt-4.1")

    result = runner.invoke(app, ["setup"])

    assert result.exit_code == 0
    assert "Setup complete:" in result.output
    assert load_credential("openai", "api_key") == "new-key"


def test_setup_flow_creates_kimi_profile_with_profile_scoped_key(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    monkeypatch.setenv("RESEARCHX_CONFIG_DIR", str(tmp_path))
    # Prevent env var leakage from overriding the configured api_key
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    monkeypatch.delenv("RESEARCHX_MOONSHOT_API_KEY", raising=False)
    monkeypatch.setattr("researchx.auth.storage._keyring_available", lambda: False)

    selections = iter(["claude-api", "kimi-anthropic"])
    prompts = iter(
        [
            "https://api.moonshot.cn/anthropic",
            "kimi-k2.5",
        ]
    )

    monkeypatch.setattr(
        "researchx.cli._select_setup_workflow", lambda *args, **kwargs: next(selections)
    )
    monkeypatch.setattr("researchx.cli._select_from_menu", lambda *args, **kwargs: next(selections))
    monkeypatch.setattr("researchx.cli._text_prompt", lambda *args, **kwargs: next(prompts))
    monkeypatch.setattr("researchx.auth.flows.ApiKeyFlow.run", lambda self: "sk-kimi-test")

    result = runner.invoke(app, ["setup"])
    assert result.exit_code == 0
    assert "Setup complete:" in result.output
    assert "- profile: kimi-anthropic" in result.output

    settings = load_settings()
    assert settings.active_profile == "kimi-anthropic"
    profile = settings.resolve_profile()[1]
    assert profile.base_url == "https://api.moonshot.cn/anthropic"
    assert profile.credential_slot == "kimi-anthropic"
    assert profile.allowed_models == ["kimi-k2.5"]

    from researchx.auth.storage import load_credential

    assert load_credential("profile:kimi-anthropic", "api_key") == "sk-kimi-test"


def test_provider_add_can_store_profile_api_key(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    monkeypatch.setenv("RESEARCHX_CONFIG_DIR", str(tmp_path))

    from researchx.auth.storage import load_credential

    result = runner.invoke(
        app,
        [
            "provider",
            "add",
            "custom-openai",
            "--label",
            "Custom OpenAI",
            "--provider",
            "openai",
            "--api-format",
            "openai",
            "--auth-source",
            "openai_api_key",
            "--model",
            "gpt-4.1",
            "--credential-slot",
            "custom-openai",
            "--api-key",
            "new-key",
        ],
    )

    assert result.exit_code == 0
    assert "API key set" in result.output
    assert load_credential("profile:custom-openai", "api_key") == "new-key"


def test_provider_edit_can_replace_profile_api_key(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    monkeypatch.setenv("RESEARCHX_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    from researchx.auth.manager import AuthManager
    from researchx.auth.storage import load_credential

    manager = AuthManager()
    manager.store_profile_credential("openai-compatible", "api_key", "old-key")

    result = runner.invoke(app, ["provider", "edit", "openai-compatible", "--api-key", "new-key"])

    assert result.exit_code == 0
    assert "API key replaced" in result.output
    assert load_credential("openai", "api_key") == "new-key"


def test_bare_cli_shows_web_instructions_without_starting_model():
    result = CliRunner().invoke(app, [])
    assert result.exit_code == 0
    assert "oh web" in result.output


def test_terminal_coding_entrypoints_are_removed():
    for arguments in (
        ["--print", "research"],
        ["--task-worker"],
        ["--continue"],
        ["--dry-run"],
        ["autopilot", "list"],
    ):
        result = CliRunner().invoke(app, arguments)
        assert result.exit_code == 2


def test_tavily_credentials_without_model_profile(tmp_path, monkeypatch):
    from researchx.auth import storage
    from researchx.api.tavily_search import resolve_tavily_key

    monkeypatch.setenv("RESEARCHX_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("RESEARCHX_TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(storage, "_keyring_available", lambda: False)
    monkeypatch.setattr(
        "researchx.auth.flows.ApiKeyFlow.run", lambda self: "test-tavily-credential"
    )
    before = load_settings().model_dump()
    runner = CliRunner()
    result = runner.invoke(app, ["auth", "login", "tavily"])
    assert result.exit_code == 0 and "saved" in result.output
    assert "test-tavily-credential" not in result.output
    assert resolve_tavily_key() == "test-tavily-credential"
    assert load_settings().model_dump() == before
    result = runner.invoke(app, ["auth", "status"])
    assert result.exit_code == 0 and "Tavily — configured" in result.output
    assert "test-tavily-credential" not in result.output
    result = runner.invoke(app, ["auth", "logout", "tavily"])
    assert result.exit_code == 0 and not resolve_tavily_key()
