"""CLI entry point using typer."""

from __future__ import annotations
from researchx.config.settings import ProviderProfile
from researchx.auth.manager import AuthManager

import json
import os
import sys
from typing import Optional

import typer

from researchx.evaluation.cli import app as eval_app
from researchx.services.execution.cli import app as operations_app

__version__ = "0.1.9"


def _version_callback(value: bool) -> None:
    if value:
        print(f"ResearchX {__version__}")
        raise typer.Exit()


app = typer.Typer(
    name="rx",
    help="ResearchX 投研工作台：使用 rx web 启动浏览器界面。",
    add_completion=False,
    rich_markup_mode="rich",
    invoke_without_command=True,
)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

mcp_app = typer.Typer(name="mcp", help="Manage MCP servers")
auth_app = typer.Typer(name="auth", help="Manage authentication")
provider_app = typer.Typer(name="provider", help="Manage provider profiles")
config_app = typer.Typer(name="config", help="Show or update settings")

app.add_typer(mcp_app)
app.add_typer(auth_app)
app.add_typer(provider_app)
app.add_typer(config_app)
app.add_typer(eval_app)
app.add_typer(operations_app)


def _default_web_port() -> int:
    """Resolve the default Web port, overridable via RESEARCHX_WEB_PORT.

    Allows moving the workspace off a busy port without passing --port each time.
    Invalid or out-of-range values fall back to the built-in default.
    """
    raw = os.environ.get("RESEARCHX_WEB_PORT", "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if 1 <= value <= 65535:
            return value
    return 8000


@app.command("web")
def web_cmd(
    port: int = typer.Option(
        _default_web_port(), "--port", min=1, max=65535, help="Local HTTP port"
    ),
    cwd: Optional[str] = typer.Option(None, "--cwd", help="Agent working directory"),
) -> None:
    """Start the local financial research web workspace."""
    try:
        import uvicorn
        from researchx.web.app import create_app
    except ImportError:
        typer.echo('Web dependencies missing. Install with: pip install -e ".[web]"', err=True)
        raise typer.Exit(1) from None
    typer.echo(f"ResearchX 投研工作台: http://127.0.0.1:{port}")
    uvicorn.run(create_app(cwd=cwd), host="127.0.0.1", port=port)


# ---- mcp subcommands ----


@mcp_app.command("list")
def mcp_list() -> None:
    """List configured MCP servers."""
    from researchx.config import load_settings
    from researchx.mcp.config import load_mcp_server_configs

    settings = load_settings()
    configs = load_mcp_server_configs(settings)
    if not configs:
        print("No MCP servers configured.")
        return
    for name, cfg in configs.items():
        transport = cfg.type
        print(f"  {name}: {transport}")


@mcp_app.command("add")
def mcp_add(
    name: str = typer.Argument(..., help="Server name"),
    config_json: str = typer.Argument(..., help="Server config as JSON string"),
) -> None:
    """Add an MCP server configuration."""
    from researchx.config import load_settings, save_settings

    settings = load_settings()
    try:
        cfg = json.loads(config_json)
    except json.JSONDecodeError as exc:
        print(f"Invalid JSON: {exc}", file=sys.stderr)
        raise typer.Exit(1)
    if not isinstance(settings.mcp_servers, dict):
        settings.mcp_servers = {}
    settings.mcp_servers[name] = cfg
    save_settings(settings)
    print(f"Added MCP server: {name}")


@mcp_app.command("remove")
def mcp_remove(
    name: str = typer.Argument(..., help="Server name to remove"),
) -> None:
    """Remove an MCP server configuration."""
    from researchx.config import load_settings, save_settings

    settings = load_settings()
    if not isinstance(settings.mcp_servers, dict) or name not in settings.mcp_servers:
        print(f"MCP server not found: {name}", file=sys.stderr)
        raise typer.Exit(1)
    del settings.mcp_servers[name]
    save_settings(settings)
    print(f"Removed MCP server: {name}")


# ---- auth subcommands ----

# Mapping from provider name to human-readable label for interactive prompts.
_PROVIDER_LABELS: dict[str, str] = {
    "anthropic": "Anthropic (Claude API)",
    "anthropic_claude": "Claude subscription (Claude CLI)",
    "openai": "OpenAI / compatible",
    "openai_codex": "OpenAI Codex subscription (Codex CLI)",
    "copilot": "GitHub Copilot",
    "dashscope": "Alibaba DashScope",
    "bedrock": "AWS Bedrock",
    "vertex": "Google Vertex AI",
    "moonshot": "Moonshot (Kimi)",
    "gemini": "Google Gemini",
    "minimax": "MiniMax",
    "modelscope": "ModelScope",
}

_AUTH_SOURCE_LABELS: dict[str, str] = {
    "anthropic_api_key": "Anthropic API key",
    "openai_api_key": "OpenAI API key",
    "codex_subscription": "Codex subscription",
    "claude_subscription": "Claude subscription",
    "copilot_oauth": "GitHub Copilot OAuth",
    "dashscope_api_key": "DashScope API key",
    "bedrock_api_key": "Bedrock credentials",
    "vertex_api_key": "Vertex credentials",
    "moonshot_api_key": "Moonshot API key",
    "gemini_api_key": "Gemini API key",
    "minimax_api_key": "MiniMax API key",
    "modelscope_api_key": "ModelScope API key",
}


def _can_use_questionary() -> bool:
    """Return True when a real interactive terminal is available."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    if sys.stdin is not sys.__stdin__ or sys.stdout is not sys.__stdout__:
        return False
    try:
        import questionary  # noqa: F401
    except ImportError:
        return False
    return True


def _select_with_questionary(
    title: str,
    options: list[tuple[str, str]],
    *,
    default_value: str | None = None,
) -> str:
    import questionary

    choices = [
        questionary.Choice(
            title=label,
            value=value,
            checked=(value == default_value),
        )
        for value, label in options
    ]
    result = questionary.select(title, choices=choices, default=default_value).ask()
    if result is None:
        raise typer.Abort()
    return str(result)


def _text_prompt(message: str, *, default: str = "") -> str:
    """Prompt for text input, preferring questionary in a real TTY."""
    if _can_use_questionary():
        import questionary

        result = questionary.text(message, default=default).ask()
        if result is None:
            raise typer.Abort()
        return str(result)
    return str(typer.prompt(message, default=default))


def _secret_prompt(message: str) -> str:
    """Prompt for secret text, preferring questionary in a real TTY."""
    if _can_use_questionary():
        import questionary

        result = questionary.password(message).ask()
        if result is None:
            raise typer.Abort()
        return str(result)
    return str(typer.prompt(message, hide_input=True))


def _confirm_prompt(message: str, *, default: bool = False) -> bool:
    """Prompt for a yes/no confirmation, preferring questionary in a real TTY."""
    if _can_use_questionary():
        import questionary

        result = questionary.confirm(message, default=default).ask()
        if result is None:
            raise typer.Abort()
        return bool(result)
    return bool(typer.confirm(message, default=default))


def _select_from_menu(
    title: str,
    options: list[tuple[str, str]],
    *,
    default_value: str | None = None,
) -> str:
    """Render a simple numbered picker and return the selected value."""
    if _can_use_questionary():
        return _select_with_questionary(title, options, default_value=default_value)
    print(title, flush=True)
    default_index = 1
    for index, (value, label) in enumerate(options, 1):
        marker = " (default)" if value == default_value else ""
        if value == default_value:
            default_index = index
        print(f"  {index}. {label}{marker}", flush=True)
    raw = typer.prompt("Choose", default=str(default_index))
    try:
        selected = options[int(raw) - 1]
    except (ValueError, IndexError):
        raise typer.BadParameter(f"Invalid selection: {raw}") from None
    return selected[0]


def _prompt_model_for_profile(profile: ProviderProfile) -> str:
    from researchx.config.settings import (
        CLAUDE_MODEL_ALIAS_OPTIONS,
        display_model_setting,
        is_claude_family_provider,
    )

    current = display_model_setting(profile)
    if profile.allowed_models:
        if len(profile.allowed_models) == 1:
            return str(profile.allowed_models[0])
        options = [(value, value) for value in profile.allowed_models]
        return _select_from_menu(
            "Choose a model setting:",
            options,
            default_value=current
            if current in profile.allowed_models
            else profile.allowed_models[0],
        )
    if is_claude_family_provider(profile.provider):
        options = [
            (value, f"{label} - {description}")
            for value, label, description in CLAUDE_MODEL_ALIAS_OPTIONS
        ]
        options.append(("__custom__", "Custom model ID"))
        selection = _select_from_menu(
            "Choose a model setting:",
            options,
            default_value=current
            if any(value == current for value, _, _ in CLAUDE_MODEL_ALIAS_OPTIONS)
            else "__custom__",
        )
        if selection != "__custom__":
            return selection
    return _text_prompt("Model", default=current).strip() or current


def _format_profile_choice_label(info: dict[str, object]) -> str:
    """Render a user-facing workflow label without leaking internal provider ids."""
    label = str(info["label"])
    state = "" if bool(info["configured"]) else f" ({info['auth_state']})"
    return f"{label}{state}"


def _styled_missing_suffix(info: dict[str, object]) -> tuple[str, str] | None:
    """Return a soft red missing-auth suffix for questionary titles."""
    if bool(info["configured"]):
        return None
    return (f" ({info['auth_state']})", "fg:#d3869b")


def _select_setup_workflow(
    statuses: dict[str, dict[str, object]],
    *,
    default_value: str | None = None,
) -> str:
    """Render the top-level `oh setup` workflow picker with richer hints."""
    hints = {
        "claude-api": ("Claude / Kimi / GLM / MiniMax", "fg:#7aa2f7"),
        "openai-compatible": ("OpenAI / OpenRouter", "fg:#9ece6a"),
    }

    if _can_use_questionary():
        import questionary

        choices = []
        for name, info in statuses.items():
            label = str(info["label"])
            hint = hints.get(name)
            missing = _styled_missing_suffix(info)
            if hint is None:
                if missing is None:
                    title: str | list[tuple[str, str]] = label
                else:
                    suffix, suffix_style = missing
                    title = [("", label), (suffix_style, suffix)]
            else:
                hint_text, hint_style = hint
                if missing is None:
                    title = [
                        ("", f"{label}  "),
                        (hint_style, hint_text),
                    ]
                else:
                    suffix, suffix_style = missing
                    title = [
                        ("", f"{label}  "),
                        (hint_style, hint_text),
                        ("", "  "),
                        (suffix_style, suffix.strip()),
                    ]
            choices.append(
                questionary.Choice(title=title, value=name, checked=(name == default_value))
            )

        result = questionary.select(
            "Choose a provider workflow:", choices=choices, default=default_value
        ).ask()
        if result is None:
            raise typer.Abort()
        return str(result)

    options: list[tuple[str, str]] = []
    for name, info in statuses.items():
        label = _format_profile_choice_label(info)
        hint = hints.get(name)
        if hint is not None:
            label = f"{label} ({hint[0]})"
        options.append((name, label))
    return _select_from_menu("Choose a provider workflow:", options, default_value=default_value)


def _default_credential_slot_for_profile(name: str, auth_source: str) -> str | None:
    from researchx.config.settings import auth_source_uses_api_key, builtin_provider_profile_names

    if name in builtin_provider_profile_names():
        return None
    if not auth_source_uses_api_key(auth_source):
        return None
    return name


def _prompt_api_key_for_profile(label: str) -> str:
    key = _secret_prompt(f"Enter API key for {label}").strip()
    if not key:
        raise typer.BadParameter("API key cannot be empty.")
    return key


def _configure_custom_profile_via_setup(manager: AuthManager) -> str:
    from researchx.config.settings import ProviderProfile, default_auth_source_for_provider

    family = _select_from_menu(
        "Choose a compatible API family:",
        [
            ("anthropic", "Anthropic-compatible"),
            ("openai", "OpenAI-compatible"),
        ],
        default_value="anthropic",
    )
    default_name = f"custom-{family}"
    name = _text_prompt("Profile name", default=default_name).strip()
    if not name:
        raise typer.BadParameter("Profile name cannot be empty.")
    label = _text_prompt("Display label", default=name).strip() or name
    base_url = _text_prompt("Base URL", default="").strip()
    if not base_url:
        raise typer.BadParameter("Base URL cannot be empty.")

    auth_source = default_auth_source_for_provider(family, family)
    model = _text_prompt("Default model", default="").strip()
    if not model:
        raise typer.BadParameter("Default model cannot be empty.")

    profile = ProviderProfile(
        label=label,
        provider=family,
        api_format=family,
        auth_source=auth_source,
        default_model=model,
        last_model=model,
        base_url=base_url,
        credential_slot=_default_credential_slot_for_profile(name, auth_source),
        allowed_models=[model],
    )
    manager.upsert_profile(name, profile)
    manager.store_profile_credential(name, "api_key", _prompt_api_key_for_profile(label))
    return name


def _ensure_preset_profile(
    manager: AuthManager,
    *,
    name: str,
    label: str,
    provider: str,
    api_format: str,
    auth_source: str,
    base_url: str | None,
    model: str,
    lock_model: bool,
) -> str:
    from researchx.config.settings import ProviderProfile

    existing = manager.list_profiles().get(name)
    profile = ProviderProfile(
        label=label,
        provider=provider,
        api_format=api_format,
        auth_source=auth_source,
        default_model=model,
        last_model=model,
        base_url=base_url,
        credential_slot=_default_credential_slot_for_profile(name, auth_source),
        allowed_models=[model] if lock_model else (existing.allowed_models if existing else []),
    )
    manager.upsert_profile(name, profile)
    return name


def _specialize_setup_target(manager: AuthManager, target: str) -> str:
    """Expand a top-level family choice into a concrete workflow profile."""
    from researchx.config.settings import default_auth_source_for_provider

    if target == "claude-api":
        choice = _select_from_menu(
            "Choose an Anthropic-compatible provider:",
            [
                ("claude-api", "Claude official"),
                ("kimi-anthropic", "Moonshot Kimi"),
                ("glm-anthropic", "Zhipu GLM"),
                ("minimax-anthropic", "MiniMax"),
            ],
            default_value="claude-api",
        )
        if choice == "claude-api":
            return choice
        defaults = {
            "kimi-anthropic": (
                "Kimi (Anthropic-compatible)",
                "https://api.moonshot.cn/anthropic",
                "kimi-k2.5",
            ),
            "glm-anthropic": ("GLM (Anthropic-compatible)", "", "glm-4.5"),
            "minimax-anthropic": ("MiniMax (Anthropic-compatible)", "", "MiniMax-M2.7"),
        }
        label, suggested_base_url, suggested_model = defaults[choice]
        base_url = _text_prompt("Base URL", default=suggested_base_url).strip()
        if not base_url:
            raise typer.BadParameter("Base URL cannot be empty.")
        model = _text_prompt("Model", default=suggested_model).strip()
        if not model:
            raise typer.BadParameter("Model cannot be empty.")
        return _ensure_preset_profile(
            manager,
            name=choice,
            label=label,
            provider="anthropic",
            api_format="anthropic",
            auth_source=default_auth_source_for_provider("anthropic", "anthropic"),
            base_url=base_url,
            model=model,
            lock_model=True,
        )

    if target == "openai-compatible":
        choice = _select_from_menu(
            "Choose an OpenAI-compatible provider:",
            [
                ("openai-compatible", "OpenAI official"),
                ("openrouter", "OpenRouter"),
            ],
            default_value="openai-compatible",
        )
        if choice == "openai-compatible":
            return choice
        base_url = _text_prompt("Base URL", default="https://openrouter.ai/api/v1").strip()
        if not base_url:
            raise typer.BadParameter("Base URL cannot be empty.")
        model = _text_prompt("Default model", default="").strip()
        if not model:
            raise typer.BadParameter("Default model cannot be empty.")
        return _ensure_preset_profile(
            manager,
            name="openrouter",
            label="OpenRouter",
            provider="openai",
            api_format="openai",
            auth_source=default_auth_source_for_provider("openai", "openai"),
            base_url=base_url,
            model=model,
            lock_model=False,
        )

    return target


def _ensure_profile_auth(manager: AuthManager, profile_name: str) -> None:
    from researchx.auth.flows import ApiKeyFlow
    from researchx.config.settings import auth_source_provider_name, auth_source_uses_api_key

    profile = manager.list_profiles()[profile_name]
    if not auth_source_uses_api_key(profile.auth_source):
        _login_provider(auth_source_provider_name(profile.auth_source))
        return

    flow = ApiKeyFlow(
        provider=profile.provider,
        prompt_text=f"Enter API key for {profile.label}",
    )
    try:
        key = flow.run()
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise typer.Exit(1)
    manager.store_profile_credential(profile_name, "api_key", key)
    print(f"{profile.label} API key saved.", flush=True)


def _maybe_update_profile_auth(manager: AuthManager, profile_name: str) -> bool:
    """Ask whether to replace an already configured profile API key."""
    from researchx.config.settings import auth_source_uses_api_key

    profile = manager.list_profiles()[profile_name]
    if not auth_source_uses_api_key(profile.auth_source):
        return False
    if not _confirm_prompt(f"Update API key for {profile.label}?", default=False):
        return False
    _ensure_profile_auth(manager, profile_name)
    return True


def _maybe_update_default_model_for_provider(provider: str) -> None:
    """Keep the active model in-family after switching auth providers."""
    from researchx.auth.manager import AuthManager

    manager = AuthManager()
    profile_name = {
        "openai_codex": "codex",
        "anthropic_claude": "claude-subscription",
    }.get(provider)
    if profile_name is None:
        return
    profile = manager.list_profiles()[profile_name]
    model = profile.resolved_model.lower()
    target_model = None
    if provider == "openai_codex" and not model.startswith(("gpt-", "o1", "o3", "o4")):
        target_model = "gpt-5.4"
    elif provider == "anthropic_claude" and not model.startswith("claude-"):
        target_model = "sonnet"
    if not target_model:
        return
    manager.update_profile(profile_name, default_model=target_model, last_model=target_model)


def _bind_external_provider(provider: str) -> None:
    """Bind a provider to credentials managed by an external CLI."""
    from researchx.auth.external import default_binding_for_provider, load_external_credential
    from researchx.auth.storage import store_external_binding

    binding = default_binding_for_provider(provider)
    try:
        credential = load_external_credential(
            binding,
            refresh_if_needed=(provider == "anthropic_claude"),
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr, flush=True)
        raise typer.Exit(1)

    profile_label = credential.profile_label or binding.profile_label
    store_external_binding(
        binding.__class__(
            provider=binding.provider,
            source_path=binding.source_path,
            source_kind=binding.source_kind,
            managed_by=binding.managed_by,
            profile_label=profile_label,
        )
    )

    _maybe_update_default_model_for_provider(provider)
    label = _PROVIDER_LABELS.get(provider, provider)
    profile_name = {
        "openai_codex": "codex",
        "anthropic_claude": "claude-subscription",
    }[provider]
    print(f"{label} bound from {credential.source_path}.", flush=True)
    print(f"Use `oh provider use {profile_name}` to activate it.", flush=True)


def _login_provider(provider: str) -> None:
    """Authenticate or bind the given provider."""
    from researchx.auth.flows import ApiKeyFlow
    from researchx.auth.manager import AuthManager
    from researchx.auth.storage import store_credential

    manager = AuthManager()

    if provider == "tavily":
        try:
            key = ApiKeyFlow(provider="tavily", prompt_text="Enter your Tavily API key").run()
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            raise typer.Exit(1)
        store_credential("tavily", "api_key", key)
        print("Tavily API key saved.", flush=True)
        return

    if provider == "copilot":
        _run_copilot_login()
        return

    if provider in ("openai_codex", "anthropic_claude"):
        _bind_external_provider(provider)
        return

    if provider in (
        "anthropic",
        "openai",
        "dashscope",
        "bedrock",
        "vertex",
        "moonshot",
        "gemini",
        "minimax",
        "modelscope",
    ):
        label = _PROVIDER_LABELS.get(provider, provider)
        flow = ApiKeyFlow(provider=provider, prompt_text=f"Enter your {label} API key")
        try:
            key = flow.run()
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            raise typer.Exit(1)
        store_credential(provider, "api_key", key)
        try:
            manager.store_credential(provider, "api_key", key)
        except Exception:
            pass
        print(f"{label} API key saved.", flush=True)
        return

    print(f"Unknown provider: {provider!r}. Known: {', '.join(_PROVIDER_LABELS)}", file=sys.stderr)
    raise typer.Exit(1)


@app.command("setup")
def setup_cmd(
    profile: str | None = typer.Argument(None, help="Provider profile name to configure"),
) -> None:
    """Unified setup flow: choose workflow, authenticate if needed, then set the model."""
    from researchx.auth.manager import AuthManager
    from researchx.config.settings import display_model_setting

    manager = AuthManager()
    statuses = manager.get_profile_statuses()
    if not statuses:
        print("No provider profiles available.", file=sys.stderr)
        raise typer.Exit(1)

    target = profile
    if target is None:
        target = _select_setup_workflow(
            statuses,
            default_value=manager.get_active_profile(),
        )

    target = _specialize_setup_target(manager, target)
    manager = AuthManager()
    statuses = manager.get_profile_statuses()

    if target not in statuses:
        print(f"Unknown provider profile: {target!r}", file=sys.stderr)
        raise typer.Exit(1)

    info = statuses[target]
    if not info["configured"]:
        source_label = _AUTH_SOURCE_LABELS.get(info["auth_source"], info["auth_source"])
        print(f"{info['label']} requires {source_label}.", flush=True)
        _ensure_profile_auth(manager, target)
        manager = AuthManager()
    else:
        if _maybe_update_profile_auth(manager, target):
            manager = AuthManager()

    profile_obj = manager.list_profiles()[target]
    model_setting = _prompt_model_for_profile(profile_obj)
    if model_setting.lower() == "default":
        manager.update_profile(target, last_model="")
    else:
        manager.update_profile(target, last_model=model_setting)
    manager.use_profile(target)

    updated = manager.list_profiles()[target]
    print(
        "Setup complete:\n"
        f"- profile: {target}\n"
        f"- provider: {updated.provider}\n"
        f"- auth_source: {updated.auth_source}\n"
        f"- model: {display_model_setting(updated)}",
        flush=True,
    )


@auth_app.command("login")
def auth_login(
    provider: Optional[str] = typer.Argument(
        None, help="Provider or service name (anthropic, openai, copilot, tavily, …)"
    ),
) -> None:
    """Interactively authenticate with a provider.

    Run without arguments to choose a provider from a menu.
    Supported providers: anthropic, anthropic_claude, openai, openai_codex, copilot, dashscope, bedrock, vertex, moonshot, minimax, modelscope; search service: tavily.
    """
    if provider is None:
        print("Select a provider to authenticate:", flush=True)
        labels = [*list(_PROVIDER_LABELS.items()), ("tavily", "Tavily Search")]
        for i, (name, label) in enumerate(labels, 1):
            print(f"  {i}. {label} [{name}]", flush=True)
        raw = typer.prompt("Enter number or provider name", default="1")
        try:
            idx = int(raw.strip()) - 1
            if 0 <= idx < len(labels):
                provider = labels[idx][0]
            else:
                print("Invalid selection.", file=sys.stderr)
                raise typer.Exit(1)
        except ValueError:
            provider = raw.strip()

    provider = provider.lower()
    _login_provider(provider)


@auth_app.command("status")
def auth_status_cmd() -> None:
    """Show authentication source and provider profile status."""
    from researchx.auth.manager import AuthManager

    manager = AuthManager()
    auth_sources = manager.get_auth_source_statuses()
    profiles = manager.get_profile_statuses()

    print("Auth sources:")
    print(f"{'Source':<24} {'State':<14} {'Origin':<10} Active")
    print("-" * 60)
    for name, info in auth_sources.items():
        label = _AUTH_SOURCE_LABELS.get(name, name)
        active_str = "<-- active" if info["active"] else ""
        print(f"{label:<24} {info['state']:<14} {info['source']:<10} {active_str}")
        if info.get("detail"):
            print(f"  detail: {info['detail']}")

    print()
    from researchx.api.tavily_search import resolve_tavily_key

    print(
        "Search service: Tavily — " + ("configured" if resolve_tavily_key() else "missing API key")
    )
    print("Provider profiles:")
    print(f"{'Profile':<20} {'Provider':<18} {'Auth source':<22} {'State':<12} Active")
    print("-" * 92)
    for name, info in profiles.items():
        status_str = "ready" if info["configured"] else info.get("auth_state", "missing auth")
        active_str = "<-- active" if info["active"] else ""
        print(
            f"{name:<20} {info['provider']:<18} {info['auth_source']:<22} {status_str:<12} {active_str}"
        )


@auth_app.command("logout")
def auth_logout(
    provider: Optional[str] = typer.Argument(
        None, help="Provider to log out (default: active provider)"
    ),
) -> None:
    """Clear stored authentication for a provider."""
    from researchx.auth.manager import AuthManager

    manager = AuthManager()
    if provider is None:
        target = manager.get_active_profile()
        manager.clear_profile_credential(target)
        print(f"Authentication cleared for profile: {target}", flush=True)
        return
    if provider == "tavily":
        from researchx.auth.storage import clear_provider_credentials

        clear_provider_credentials("tavily")
        print(
            "Tavily stored key cleared; environment variables, if set, remain effective.",
            flush=True,
        )
        return
    manager.clear_credential(provider)
    print(f"Authentication cleared for provider: {provider}", flush=True)


@auth_app.command("switch")
def auth_switch(
    provider: str = typer.Argument(..., help="Auth source or profile to activate"),
) -> None:
    """Switch the auth source for the active profile, or use a profile by name."""
    from researchx.auth.manager import AuthManager

    manager = AuthManager()
    try:
        manager.switch_provider(provider)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise typer.Exit(1)
    print(f"Switched auth/profile to: {provider}", flush=True)


# ---------------------------------------------------------------------------
# Copilot login helper (kept as a named function for reuse and backward compat)
# ---------------------------------------------------------------------------


def _run_copilot_login() -> None:
    """Run the GitHub Copilot device-code flow and persist the result."""
    from researchx.api.copilot_auth import save_copilot_auth
    from researchx.auth.flows import DeviceCodeFlow

    print("Select GitHub deployment type:", flush=True)
    print("  1. GitHub.com (public)", flush=True)
    print("  2. GitHub Enterprise (data residency / self-hosted)", flush=True)
    choice = typer.prompt("Enter choice", default="1")

    enterprise_url: str | None = None
    github_domain = "github.com"

    if choice.strip() == "2":
        raw_url = typer.prompt("Enter your GitHub Enterprise URL or domain (e.g. company.ghe.com)")
        domain = raw_url.replace("https://", "").replace("http://", "").rstrip("/")
        if not domain:
            print("Error: domain cannot be empty.", file=sys.stderr, flush=True)
            raise typer.Exit(1)
        enterprise_url = domain
        github_domain = domain

    print(flush=True)
    flow = DeviceCodeFlow(github_domain=github_domain, enterprise_url=enterprise_url)
    try:
        token = flow.run()
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr, flush=True)
        raise typer.Exit(1)

    save_copilot_auth(token, enterprise_url=enterprise_url)
    print("GitHub Copilot authenticated successfully.", flush=True)
    if enterprise_url:
        print(f"  Enterprise domain: {enterprise_url}", flush=True)
    print(flush=True)
    print("To use Copilot as the provider, run:", flush=True)
    print("  oh provider use copilot", flush=True)


@auth_app.command("copilot-login")
def auth_copilot_login() -> None:
    """Authenticate with GitHub Copilot via device flow (alias for 'oh auth login copilot')."""
    _run_copilot_login()


@auth_app.command("codex-login")
def auth_codex_login() -> None:
    """Bind ResearchX to a local Codex CLI subscription session."""
    _bind_external_provider("openai_codex")


@auth_app.command("claude-login")
def auth_claude_login() -> None:
    """Bind ResearchX to a local Claude CLI subscription session."""
    _bind_external_provider("anthropic_claude")


@auth_app.command("copilot-logout")
def auth_copilot_logout() -> None:
    """Remove stored GitHub Copilot authentication."""
    from researchx.api.copilot_auth import clear_github_token

    clear_github_token()
    print("Copilot authentication cleared.")


# ---- config subcommands ----


def _config_resolve_target(settings: object, key: str) -> tuple[object, str]:
    target = settings
    parts = key.split(".")
    for part in parts[:-1]:
        if not hasattr(target, part):
            raise KeyError(key)
        target = getattr(target, part)
    leaf = parts[-1]
    if not hasattr(target, leaf):
        raise KeyError(key)
    return target, leaf


def _config_coerce_value(current: object, raw: str) -> object:
    if isinstance(current, bool):
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"Invalid boolean value: {raw}")
    if isinstance(current, int) and not isinstance(current, bool):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    if isinstance(current, list):
        return [entry.strip() for entry in raw.split(",") if entry.strip()]
    return raw


@config_app.command("show")
def config_show() -> None:
    """Print the resolved settings JSON."""
    from researchx.security.redaction import _settings_json_for_display
    from researchx.config.settings import load_settings

    print(_settings_json_for_display(load_settings()), flush=True)


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Setting key, including dotted nested keys"),
    value: str = typer.Argument(..., help="Value to store"),
) -> None:
    """Persist one setting in ~/.researchx/settings.json."""
    from researchx.config.settings import load_settings, save_settings

    settings = load_settings()
    try:
        target, leaf = _config_resolve_target(settings, key)
    except KeyError:
        print(f"Unknown config key: {key}", file=sys.stderr)
        raise typer.Exit(1)
    try:
        coerced = _config_coerce_value(getattr(target, leaf), value)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise typer.Exit(1)
    setattr(target, leaf, coerced)
    save_settings(settings)
    print(f"Updated {key}", flush=True)


# ---- provider subcommands ----


@provider_app.command("list")
def provider_list() -> None:
    """List configured provider profiles."""
    from researchx.auth.manager import AuthManager

    statuses = AuthManager().get_profile_statuses()
    for name, info in statuses.items():
        marker = "*" if info["active"] else " "
        configured = "ready" if info["configured"] else "missing auth"
        base = info["base_url"] or "(default)"
        print(f"{marker} {name}: {info['label']} [{configured}]")
        print(f"    auth={info['auth_source']} model={info['model']} base_url={base}")


@provider_app.command("use")
def provider_use(
    name: str = typer.Argument(..., help="Provider profile name"),
) -> None:
    """Activate a provider profile."""
    from researchx.auth.manager import AuthManager

    manager = AuthManager()
    try:
        manager.use_profile(name)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise typer.Exit(1)
    print(f"Activated provider profile: {name}", flush=True)


@provider_app.command("add")
def provider_add(
    name: str = typer.Argument(..., help="Provider profile name"),
    label: str = typer.Option(..., "--label", help="Display label"),
    provider: str = typer.Option(..., "--provider", help="Runtime provider id"),
    api_format: str = typer.Option(..., "--api-format", help="API format"),
    auth_source: str = typer.Option(..., "--auth-source", help="Auth source name"),
    model: str = typer.Option(..., "--model", help="Default model"),
    base_url: str | None = typer.Option(None, "--base-url", help="Optional base URL"),
    credential_slot: str | None = typer.Option(
        None, "--credential-slot", help="Optional profile-specific credential slot"
    ),
    api_key: str | None = typer.Option(None, "--api-key", help="Set the profile API key"),
    allowed_models: list[str] | None = typer.Option(
        None, "--allowed-model", help="Allowed model values for this profile"
    ),
    context_window_tokens: int | None = typer.Option(
        None, "--context-window-tokens", help="Optional context window override for auto-compact"
    ),
    auto_compact_threshold_tokens: int | None = typer.Option(
        None,
        "--auto-compact-threshold-tokens",
        help="Optional explicit auto-compact threshold override",
    ),
) -> None:
    """Create a provider profile."""
    from researchx.auth.manager import AuthManager
    from researchx.config.settings import ProviderProfile

    manager = AuthManager()
    manager.upsert_profile(
        name,
        ProviderProfile(
            label=label,
            provider=provider,
            api_format=api_format,
            auth_source=auth_source,
            default_model=model,
            last_model=model,
            base_url=base_url,
            credential_slot=credential_slot
            or _default_credential_slot_for_profile(name, auth_source),
            allowed_models=allowed_models
            or (
                [model]
                if credential_slot or _default_credential_slot_for_profile(name, auth_source)
                else []
            ),
            context_window_tokens=context_window_tokens,
            auto_compact_threshold_tokens=auto_compact_threshold_tokens,
        ),
    )
    if api_key is not None:
        manager = AuthManager()
        manager.store_profile_credential(name, "api_key", api_key)
        print(f"Saved provider profile: {name} (API key set)", flush=True)
    else:
        print(f"Saved provider profile: {name}", flush=True)


@provider_app.command("edit")
def provider_edit(
    name: str = typer.Argument(..., help="Provider profile name"),
    label: str | None = typer.Option(None, "--label", help="Display label"),
    provider: str | None = typer.Option(None, "--provider", help="Runtime provider id"),
    api_format: str | None = typer.Option(None, "--api-format", help="API format"),
    auth_source: str | None = typer.Option(None, "--auth-source", help="Auth source name"),
    model: str | None = typer.Option(None, "--model", help="Default model"),
    base_url: str | None = typer.Option(None, "--base-url", help="Optional base URL"),
    credential_slot: str | None = typer.Option(
        None, "--credential-slot", help="Optional profile-specific credential slot"
    ),
    api_key: str | None = typer.Option(None, "--api-key", help="Replace the profile API key"),
    allowed_models: list[str] | None = typer.Option(
        None, "--allowed-model", help="Allowed model values for this profile"
    ),
    context_window_tokens: int | None = typer.Option(
        None, "--context-window-tokens", help="Optional context window override for auto-compact"
    ),
    auto_compact_threshold_tokens: int | None = typer.Option(
        None,
        "--auto-compact-threshold-tokens",
        help="Optional explicit auto-compact threshold override",
    ),
) -> None:
    """Edit a provider profile."""
    from researchx.auth.manager import AuthManager

    manager = AuthManager()
    try:
        manager.update_profile(
            name,
            label=label,
            provider=provider,
            api_format=api_format,
            auth_source=auth_source,
            default_model=model,
            last_model=model,
            base_url=base_url,
            credential_slot=credential_slot,
            allowed_models=allowed_models,
            context_window_tokens=context_window_tokens,
            auto_compact_threshold_tokens=auto_compact_threshold_tokens,
        )
        if api_key is not None:
            manager = AuthManager()
            manager.store_profile_credential(name, "api_key", api_key)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise typer.Exit(1)
    if api_key is not None:
        print(f"Updated provider profile: {name} (API key replaced)", flush=True)
    else:
        print(f"Updated provider profile: {name}", flush=True)


@provider_app.command("remove")
def provider_remove(
    name: str = typer.Argument(..., help="Provider profile name"),
) -> None:
    """Remove a provider profile."""
    from researchx.auth.manager import AuthManager

    manager = AuthManager()
    try:
        manager.remove_profile(name)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise typer.Exit(1)
    print(f"Removed provider profile: {name}", flush=True)


# ---------------------------------------------------------------------------
# Main command
# ---------------------------------------------------------------------------


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        "-v",
        help="Show version and exit",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Manage the research workspace; no terminal coding conversation is launched."""
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        typer.echo("启动投研工作台：oh web")
