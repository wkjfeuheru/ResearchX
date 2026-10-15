"""Investment research runtime, independent of its Web transport."""

from __future__ import annotations
from researchx.config import Settings
from researchx.engine.metadata import ExecutionMetadata
from researchx.config.settings import ResolvedAuth
from researchx.state.store import ResearchStore
from researchx.evaluation.observer import RecordingObserver
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable
import sys
import asyncio
from contextlib import AsyncExitStack
from researchx.services.execution.async_timeout import timeout as async_timeout
from uuid import uuid4
from researchx.api.client import AnthropicApiClient, SupportsStreamingMessages
from researchx.api.codex_client import CodexApiClient
from researchx.api.copilot_client import CopilotClient
from researchx.api.openai_client import OpenAICompatibleClient
from researchx.config import load_settings
from researchx.engine import QueryEngine
from researchx.engine.messages import ConversationMessage, sanitize_conversation_messages
from researchx.engine.stream_events import StreamEvent
from researchx.hooks import HookEvent, HookExecutionContext, HookExecutor, load_hook_registry
from researchx.mcp.client import McpClientManager
from researchx.mcp.config import load_mcp_server_configs
from researchx.permissions import PermissionChecker
from researchx.prompts import build_runtime_prompt
from researchx.services.sessions.storage import _persistable_tool_metadata
from researchx.tools import ToolRegistry, create_research_tool_registry

PermissionPrompt = Callable[[str, str], Awaitable[bool]]
AskUserPrompt = Callable[[str], Awaitable[str]]
EditApprovalPrompt = Callable[[str, str, int, int], Awaitable[str]]
StreamRenderer = Callable[[StreamEvent], Awaitable[None]]


def _resolve_vision_config(settings: Settings) -> dict[str, str]:
    """Resolve the vision model configuration from settings or environment.

    Priority: settings.vision fields > environment variables > empty.
    """
    from researchx.config.settings import VisionModelConfig

    cfg = settings.vision
    if cfg.is_configured:
        return {
            "model": cfg.model,
            "api_key": cfg.api_key,
            "base_url": cfg.base_url,
        }

    # Fall back to environment variables
    env_cfg = VisionModelConfig.from_env()
    if env_cfg.is_configured:
        return {
            "model": env_cfg.model,
            "api_key": env_cfg.api_key,
            "base_url": env_cfg.base_url,
        }

    return {}


@dataclass
class RuntimeBundle:
    api_client: SupportsStreamingMessages
    cwd: str
    mcp_manager: McpClientManager
    tool_registry: ToolRegistry
    hook_executor: HookExecutor
    engine: QueryEngine
    session_id: str
    runtime_id: str = ""
    settings_overrides: dict[str, Any] = field(default_factory=dict)
    extra_skill_dirs: tuple[str, ...] = ()
    resources: AsyncExitStack | None = field(default=None, repr=False)
    owns_api_client: bool = True
    session_started: bool = False
    close_task: asyncio.Task[None] | None = field(default=None, repr=False)
    cleanup_errors: list[BaseException] = field(default_factory=list, repr=False)

    def current_settings(self) -> Settings:
        return load_settings().merge_cli_overrides(**self.settings_overrides)


def _resolve_api_client_from_settings(settings: Settings) -> SupportsStreamingMessages:
    """Build the appropriate API client for the resolved settings."""
    # Ensure profile fields (base_url, model, api_format) are projected to settings
    settings = settings.materialize_active_profile()

    def _safe_resolve_auth() -> ResolvedAuth:
        try:
            return settings.resolve_auth()
        except Exception as exc:
            _print_auth_resolution_error(settings, exc)
            raise SystemExit(1)

    if settings.api_format == "copilot":
        from researchx.api.copilot_client import COPILOT_DEFAULT_MODEL

        copilot_model = (
            COPILOT_DEFAULT_MODEL
            if settings.model
            in {"claude-sonnet-4-20250514", "claude-sonnet-4-6", "sonnet", "default"}
            else settings.model
        )
        return CopilotClient(model=copilot_model)
    if settings.provider == "openai_codex":
        auth = _safe_resolve_auth()
        return CodexApiClient(
            auth_token=auth.value,
            base_url=settings.base_url,
        )
    if settings.provider == "anthropic_claude":
        return AnthropicApiClient(
            auth_token=(_safe_resolve_auth()).value,
            base_url=settings.base_url,
            claude_oauth=True,
            auth_token_resolver=lambda: (settings.resolve_auth()).value,
        )
    if settings.api_format in ("openai", "openai_compat"):
        auth = _safe_resolve_auth()
        return OpenAICompatibleClient(
            api_key=auth.value,
            base_url=settings.base_url,
            timeout=settings.timeout,
        )
    auth = _safe_resolve_auth()
    return AnthropicApiClient(
        api_key=auth.value,
        base_url=settings.base_url,
    )


def _print_auth_resolution_error(settings: Settings, exc: Exception) -> None:
    """Render auth failures without collapsing subscription errors into API-key advice."""
    try:
        profile_name, profile = settings.resolve_profile()
        auth_source = (getattr(profile, "auth_source", "") or "").strip()
    except Exception:
        profile_name = ""
        auth_source = ""

    message = str(exc).strip() or exc.__class__.__name__
    if auth_source in {"claude_subscription", "codex_subscription"}:
        login_command = "claude-login" if auth_source == "claude_subscription" else "codex-login"
        provider_name = profile_name or (
            "claude-subscription" if auth_source == "claude_subscription" else "codex"
        )
        print(
            f"Error: {message}\n"
            f"  This profile uses subscription auth, not an API key.\n"
            f"  Run `oh auth {login_command}` to bind the local CLI session, then\n"
            f"  run `oh provider use {provider_name}` to activate it.",
            file=sys.stderr,
        )
        return

    print(
        "Error: No API key configured.\n"
        f"  {message}\n"
        "  Run `oh auth login` to set up authentication, or set the\n"
        "  ANTHROPIC_API_KEY (or OPENAI_API_KEY) environment variable.",
        file=sys.stderr,
    )


async def build_runtime(
    *,
    prompt: str | None = None,
    cwd: str | None = None,
    model: str | None = None,
    max_turns: int | None = None,
    effort: str | None = None,
    base_url: str | None = None,
    system_prompt: str | None = None,
    api_key: str | None = None,
    api_format: str | None = None,
    active_profile: str | None = None,
    api_client: SupportsStreamingMessages | None = None,
    owns_api_client: bool = True,
    permission_prompt: PermissionPrompt | None = None,
    ask_user_prompt: AskUserPrompt | None = None,
    edit_approval_prompt: EditApprovalPrompt | None = None,
    restore_usage: dict[str, object] | None = None,
    restore_messages: list[dict[str, object]] | None = None,
    restore_tool_metadata: ExecutionMetadata | None = None,
    enforce_max_turns: bool = True,
    permission_mode: str | None = None,
    extra_skill_dirs: Iterable[str | Path] | None = None,
    session_id: str | None = None,
    observer: RecordingObserver | None = None,
    settings_override: Settings | None = None,
    research_store_override: ResearchStore | None = None,
    connect_mcp: bool = True,
    context_window_tokens: int | None = None,
) -> RuntimeBundle:
    """Build the shared runtime for an ResearchX session."""
    pending = AsyncExitStack()
    cleanup_errors: list[BaseException] = []
    try:
        if api_client is not None and owns_api_client:
            close = getattr(api_client, "close", None)
            if close is not None:
                pending.push_async_callback(_close_resource, "api_client", close, cleanup_errors)
        settings_overrides: dict[str, Any] = {
            "model": model,
            "max_turns": max_turns,
            "effort": effort,
            "base_url": base_url,
            "system_prompt": system_prompt,
            "api_key": api_key,
            "api_format": api_format,
            "active_profile": active_profile,
            "permission_mode": permission_mode,
            "context_window_tokens": context_window_tokens,
        }
        settings = (settings_override or load_settings()).merge_cli_overrides(**settings_overrides)
        cwd = str(Path(cwd).expanduser().resolve()) if cwd else str(Path.cwd())
        normalized_skill_dirs = tuple(
            str(Path(path).expanduser().resolve()) for path in (extra_skill_dirs or ())
        )
        resolved_api_client = (
            api_client if api_client is not None else _resolve_api_client_from_settings(settings)
        )
        client_is_owned = api_client is None or owns_api_client
        if api_client is None:
            close = getattr(resolved_api_client, "close", None)
            if close is not None:
                pending.push_async_callback(_close_resource, "api_client", close, cleanup_errors)
        if observer is not None:
            from researchx.evaluation.observer import ObservedClient

            resolved_api_client = ObservedClient(resolved_api_client, observer)
        mcp_manager = McpClientManager(load_mcp_server_configs(settings))
        pending.push_async_callback(_close_resource, "mcp", mcp_manager.close, cleanup_errors)
        if connect_mcp:
            await mcp_manager.connect_all()
        tool_registry = create_research_tool_registry(mcp_manager)
        hook_executor = HookExecutor(
            load_hook_registry(settings),
            HookExecutionContext(
                cwd=Path(cwd).resolve(),
                settings=settings,
                api_client=resolved_api_client,
                default_model=settings.model,
                context_window_tokens=settings.context_window_tokens,
            ),
        )
        engine_max_turns = (
            settings.max_turns if (enforce_max_turns or max_turns is not None) else None
        )
        system_prompt_text = build_runtime_prompt(
            settings,
            cwd=cwd,
            latest_user_prompt=prompt,
            extra_skill_dirs=normalized_skill_dirs,
        )
        if not session_id:
            raise ValueError("Research runtime requires the existing Web session ID")
        restored_metadata = _persistable_tool_metadata(restore_tool_metadata)
        if observer is not None:
            restored_metadata["observer"] = observer
        restored_metadata["permission_mode"] = settings.permission.mode.value
        from researchx.state.store import ResearchStore

        store = research_store_override or ResearchStore(cwd, session_id)
        (await store.load())  # Corruption must be reported, never silently reset.
        restored_metadata["research_store"] = store
        restored_metadata["research_auto_inject"] = settings.research.context.auto_inject
        restored_metadata["research_injection_budget"] = (
            settings.research_memory.injection_budget_tokens
        )
        restored_metadata["conflict_max_turns"] = settings.research_memory.conflict_max_turns
        restored_metadata["conflict_timeout_seconds"] = (
            settings.research_memory.conflict_timeout_seconds
        )
        restored_metadata["research_workspace_root"] = settings.research_memory.workspace_root
        restored_metadata["memory_auto_inject_max_chars"] = (
            settings.research_memory.memory_auto_inject_max_chars
        )
        restored_metadata["auto_summary_max_tokens"] = (
            settings.research_memory.auto_summary_max_tokens
        )
        restored_metadata["auto_index_max_items"] = settings.research_memory.auto_index_max_items
        restored_metadata["query_default_limit"] = settings.research_memory.query_default_limit
        restored_metadata["query_max_limit"] = settings.research_memory.query_max_limit
        restored_metadata["subagent_max_concurrency"] = (
            settings.research_memory.subagent_max_concurrency
        )
        restored_metadata["subagent_max_calls"] = settings.research_memory.subagent_max_calls
        restored_metadata["subagent_timeout_seconds"] = (
            settings.research_memory.subagent_timeout_seconds
        )

        runtime_id = uuid4().hex
        from researchx.sandbox.session import stop_runtime_sandboxes

        pending.push_async_callback(
            _close_resource, "sandbox", lambda: stop_runtime_sandboxes(runtime_id), cleanup_errors
        )
        engine = QueryEngine(
            api_client=resolved_api_client,
            tool_registry=tool_registry,
            permission_checker=PermissionChecker(settings.permission),
            cwd=cwd,
            model=settings.model,
            system_prompt=system_prompt_text,
            max_tokens=settings.max_tokens,
            context_window_tokens=settings.context_window_tokens,
            auto_compact_threshold_tokens=settings.auto_compact_threshold_tokens,
            max_turns=engine_max_turns,
            permission_prompt=permission_prompt,
            ask_user_prompt=ask_user_prompt,
            hook_executor=hook_executor,
            settings=settings,
            tool_metadata={
                "trusted_settings": settings.model_copy(deep=True),
                "runtime_id": runtime_id,
                "mcp_manager": mcp_manager,
                "extra_skill_dirs": normalized_skill_dirs,
                "session_id": session_id,
                "edit_approval_prompt": edit_approval_prompt,
                "vision_model_config": _resolve_vision_config(settings),
                **restored_metadata,
            },
        )
        hook_executor._context.account_usage = engine.tool_metadata.get("account_subagent_usage")
        engine.restore_usage(restore_usage)
        # Restore messages from a saved session if provided
        if restore_messages:
            (
                await engine.load_messages(
                    [ConversationMessage.model_validate(m) for m in restore_messages]
                )
            )
            (await engine.load_messages(sanitize_conversation_messages(engine.messages)))

        # Start Docker sandbox if configured
        sandbox_store = engine.tool_metadata.get("research_store")
        if (
            settings.sandbox.enabled
            and settings.sandbox.backend == "docker"
            and (sandbox_store is None or (await sandbox_store.load()).project is None)
        ):
            from researchx.sandbox.session import start_docker_sandbox

            await start_docker_sandbox(settings, runtime_id, Path(cwd))

        bundle = RuntimeBundle(
            api_client=resolved_api_client,
            cwd=cwd,
            mcp_manager=mcp_manager,
            tool_registry=tool_registry,
            hook_executor=hook_executor,
            engine=engine,
            session_id=session_id,
            runtime_id=runtime_id,
            settings_overrides=settings_overrides,
            extra_skill_dirs=normalized_skill_dirs,
            owns_api_client=client_is_owned,
            cleanup_errors=cleanup_errors,
        )
        bundle.resources = pending.pop_all()
        return bundle
    except BaseException as exc:
        cleanup = asyncio.create_task(pending.aclose())
        try:
            await _await_cleanup(cleanup)
        except asyncio.CancelledError as cleanup_cancel:
            cleanup_errors.append(cleanup_cancel)
        if cleanup_errors:
            setattr(exc, "cleanup_errors", cleanup_errors)
        raise


async def start_runtime(bundle: RuntimeBundle) -> None:
    """Run session start hooks."""
    from researchx.api.retry import bind_api_audit

    bundle.session_started = True
    with bind_api_audit(bundle.cwd, bundle.session_id):
        await bundle.hook_executor.execute(
            HookEvent.SESSION_START,
            {"cwd": bundle.cwd, "event": HookEvent.SESSION_START.value},
        )


class RuntimeCleanupError(RuntimeError):
    def __init__(self, errors: list[BaseException]) -> None:
        self.errors = errors
        super().__init__(
            "Runtime cleanup failed: "
            + ", ".join(
                f"{getattr(error, 'resource', 'resource')}: {type(error).__name__}"
                for error in errors
            )
        )


async def _close_resource(
    name: str, close: Callable[[], Awaitable[Any]], errors: list[BaseException]
) -> None:
    try:
        async with async_timeout(15):
            await close()
    except BaseException as exc:
        setattr(exc, "resource", name)
        errors.append(exc)


async def _await_cleanup(task: asyncio.Task[None]) -> None:
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError


async def close_runtime(bundle: RuntimeBundle) -> None:
    if bundle.close_task is None:
        bundle.close_task = asyncio.create_task(_close_runtime(bundle))
    await _await_cleanup(bundle.close_task)
    if bundle.cleanup_errors:
        raise RuntimeCleanupError(bundle.cleanup_errors)


async def _close_runtime(bundle: RuntimeBundle) -> None:
    from researchx.api.retry import bind_api_audit
    from researchx.sandbox.session import stop_runtime_sandboxes

    with bind_api_audit(bundle.cwd, bundle.session_id):
        if bundle.session_started:
            await _close_resource(
                "session_end",
                lambda: bundle.hook_executor.execute(
                    HookEvent.SESSION_END,
                    {"cwd": bundle.cwd, "event": HookEvent.SESSION_END.value},
                ),
                bundle.cleanup_errors,
            )
        if bundle.resources is not None:
            await bundle.resources.aclose()
        else:
            await _close_resource(
                "sandbox", lambda: stop_runtime_sandboxes(bundle.runtime_id), bundle.cleanup_errors
            )
            await _close_resource("mcp", bundle.mcp_manager.close, bundle.cleanup_errors)
            close = getattr(bundle.api_client, "close", None)
            if bundle.owns_api_client and close is not None:
                await _close_resource("api_client", close, bundle.cleanup_errors)


async def handle_line(
    bundle: RuntimeBundle,
    line: str,
    *,
    render_event: StreamRenderer,
) -> bool:
    """Submit Web text directly to the research engine."""
    async for event in bundle.engine.submit_message(line):
        await render_event(event)
    return True
