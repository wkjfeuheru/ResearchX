"""High-level conversation engine."""

from __future__ import annotations
from researchx.services.context.sources import ContextSnapshot
from researchx.state.store import ResearchStore

import asyncio
import json
import re
from pathlib import Path
from uuid import uuid4

from researchx.engine.metadata import ExecutionMetadata

from researchx.api.usage import UsageSnapshot
from researchx.prompts.context import RuntimePrompt, _build_permission_mode_section
from researchx.permissions.modes import PermissionMode
from typing import Any, AsyncGenerator, cast

from researchx.api.client import SupportsStreamingMessages
from researchx.engine.cost_tracker import CostTracker
from researchx.engine.messages import (
    ConversationMessage,
    ToolResultBlock,
    sanitize_conversation_messages,
    TextBlock,
    ContextSpan,
)
from researchx.engine.query import AskUserPrompt, PermissionPrompt, QueryContext, run_query
from researchx.engine.stream_events import AssistantTurnComplete, StreamEvent, ErrorEvent
from researchx.config.settings import Settings
from researchx.hooks import HookEvent, HookExecutor
from researchx.permissions.checker import PermissionChecker
from researchx.tools.base import ToolRegistry


class QueryEngine:
    """Owns conversation history and the tool-aware model loop."""

    def __init__(
        self,
        *,
        api_client: SupportsStreamingMessages,
        tool_registry: ToolRegistry,
        permission_checker: PermissionChecker,
        cwd: str | Path,
        model: str,
        system_prompt: str | RuntimePrompt,
        max_tokens: int = 4096,
        context_window_tokens: int | None = None,
        auto_compact_threshold_tokens: int | None = None,
        max_turns: int | None = 8,
        permission_prompt: PermissionPrompt | None = None,
        ask_user_prompt: AskUserPrompt | None = None,
        hook_executor: HookExecutor | None = None,
        tool_metadata: ExecutionMetadata | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._execution_session_id = uuid4().hex
        self._api_client = api_client
        self._tool_registry = tool_registry
        self._permission_checker = permission_checker
        self._cwd = Path(cwd).resolve()
        self._model = model
        self._runtime_context: str | None = None
        self._runtime_manifest: list[ContextSpan] | None = None
        self.set_system_prompt(system_prompt)
        self._max_tokens = max_tokens
        self._effort = settings.effort if settings is not None else None
        self._context_window_tokens = context_window_tokens
        self._auto_compact_threshold_tokens = auto_compact_threshold_tokens
        self._max_turns = max_turns
        self._permission_prompt = permission_prompt
        self._ask_user_prompt = ask_user_prompt
        self._hook_executor = hook_executor
        self._tool_metadata: ExecutionMetadata = tool_metadata or {}
        store = self._tool_metadata.get("research_store")
        if store is not None and not self._tool_metadata.get("conflict_investigator"):
            from researchx.state.runtime import ResearchAgentRuntime

            runtime = self._tool_metadata.get("research_runtime")
            if runtime is None or runtime.store is not store:
                runtime = ResearchAgentRuntime(
                    store,
                    permission_checker=permission_checker,
                    workspace_root=self._tool_metadata.get("research_workspace_root"),
                    memory_auto_inject_max_chars=self._tool_metadata.get(
                        "memory_auto_inject_max_chars", 12000
                    ),
                )
            runtime.permission_checker = permission_checker
            self._tool_metadata["research_runtime"] = runtime
        self._settings = settings
        self._messages: list[ConversationMessage] = []
        self._cost_tracker = CostTracker()
        # Account immediately, including child calls completed before timeout/cancellation.
        self._tool_metadata["account_subagent_usage"] = lambda usage: self._cost_tracker.add(usage)

    @property
    def messages(self) -> list[ConversationMessage]:
        """Return the current conversation history."""
        return list(self._messages)

    @property
    def max_turns(self) -> int | None:
        """Return the maximum number of agentic turns per user input, if capped."""
        return self._max_turns

    @property
    def api_client(self) -> SupportsStreamingMessages:
        """Return the active API client."""
        return self._api_client

    @property
    def model(self) -> str:
        """Return the active model identifier."""
        return self._model

    @property
    def system_prompt(self) -> str:
        """Return the active system prompt."""
        return self._system_prompt

    @property
    async def runtime_context(self) -> str | None:
        """Return current hidden runtime state for diagnostics."""
        return await self._current_runtime_context()

    @property
    def tool_metadata(self) -> ExecutionMetadata:
        """Return the mutable tool metadata/carry-over state."""
        return self._tool_metadata

    @property
    def total_usage(self) -> UsageSnapshot:
        """Return the total usage across all turns."""
        return self._cost_tracker.total

    def clear(self) -> None:
        """Clear the in-memory conversation history."""
        self._messages.clear()
        self._cost_tracker = CostTracker()

    def set_system_prompt(self, prompt: str | RuntimePrompt) -> None:
        """Update the active system prompt for future turns."""
        if isinstance(prompt, RuntimePrompt):
            self._system_prompt = prompt.system_prompt
            self._runtime_context = prompt.runtime_context
            self._runtime_manifest = prompt.runtime_context_manifest
        else:
            self._system_prompt = prompt

    def set_model(self, model: str) -> None:
        """Update the active model for future turns."""
        self._model = model

    def set_effort(self, effort: str | None) -> None:
        """Update the active reasoning effort for future turns."""
        self._effort = effort

    def set_api_client(self, api_client: SupportsStreamingMessages) -> None:
        """Update the active API client for future turns."""
        self._api_client = api_client

    def set_max_turns(self, max_turns: int | None) -> None:
        """Update the maximum number of agentic turns per user input."""
        self._max_turns = None if max_turns is None else max(1, int(max_turns))

    def set_permission_checker(self, checker: PermissionChecker) -> None:
        """Update the active permission checker for future turns."""
        self._permission_checker = checker
        runtime = self._tool_metadata.get("research_runtime")
        if runtime:
            runtime.permission_checker = checker

    async def _current_runtime_context(self) -> str | None:
        return (await self._current_runtime_snapshot()).text or None

    async def _current_runtime_snapshot(self) -> ContextSnapshot:
        """Refresh only mutable loop state; query-specific memory stays snapshotted."""
        from researchx.state.runtime import ResearchAgentRuntime
        from researchx.services.context.sources import (
            compose_research_context,
            runtime_fragments,
            refresh_runtime_messages,
        )
        from researchx.engine.query import _bounded_completion_tokens
        from researchx.config.context_components import ContextComponentsSettings

        original = self._runtime_context or ""
        base_manifest = self._runtime_manifest
        if base_manifest:
            cleaned = refresh_runtime_messages(
                [
                    ConversationMessage(
                        role="user",
                        runtime_context=original,
                        runtime_context_manifest=base_manifest,
                    )
                ],
                ContextSnapshot(),
                strip_memory=True,
            )
            original = cleaned[0].runtime_context or "" if cleaned else ""
            base_manifest = cleaned[0].runtime_context_manifest if cleaned else []
        mode = self._tool_metadata.get("permission_mode")
        section = None
        if mode and self._settings is not None:
            settings = self._settings.model_copy(deep=True)
            settings.permission.mode = PermissionMode(mode)
            section = (
                "<permission_mode>\n"
                + _build_permission_mode_section(settings)
                + "\n</permission_mode>"
            )

        def refresh(text: str) -> str:
            # Strip only host-owned mutable envelopes; ordinary user text is untouched.
            text = ResearchAgentRuntime.strip_workspace_context(text)
            text = re.sub(
                r"\n*<(long_term_memory|research_memory|coordinator_context)>.*?</\1>",
                "",
                text,
                flags=re.S,
            )
            if section is not None:
                text = re.sub(
                    r"<permission_mode>.*?</permission_mode>", lambda _: section, text, flags=re.S
                )
            return text

        text, manifest = refresh(original), None
        if base_manifest is not None:
            fragments, fallback = runtime_fragments(original, base_manifest)
            if not fallback:
                # Updating a rule's text must preserve its logical S attribution.
                text, manifest = "", []
                for body, component, deferrable in fragments:
                    body = refresh(body)
                    if not body:
                        continue
                    start = len(text)
                    text += body
                    manifest.append(
                        ContextSpan(
                            start=start,
                            end=len(text),
                            component=component,
                            source="runtime",
                            deferrable=deferrable,
                        )
                    )
        return await compose_research_context(
            ContextSnapshot(text, manifest or []),
            store=self._tool_metadata.get("research_store"),
            runtime=self._tool_metadata.get("research_runtime"),
            model=self._model,
            output_tokens=_bounded_completion_tokens(self._max_tokens),
            window=self._context_window_tokens,
            policy=self._settings.context_components
            if self._settings
            else ContextComponentsSettings(),
            legacy_budget=int(self._tool_metadata.get("research_injection_budget", 6000)),
            enabled=self._settings.research.context.auto_inject if self._settings else True,
            auto_summary_max_tokens=int(self._tool_metadata.get("auto_summary_max_tokens", 1500)),
            auto_index_max_items=int(self._tool_metadata.get("auto_index_max_items", 12)),
        )

    def restore_usage(self, usage: dict[str, object] | UsageSnapshot | None) -> None:
        self._cost_tracker = CostTracker(UsageSnapshot.model_validate(usage or {}))

    async def load_messages(
        self, messages: list[ConversationMessage], *, preserve_runtime_context: bool = False
    ) -> None:
        """Replace the in-memory conversation history."""
        snapshot = (await self._current_runtime_snapshot()) if preserve_runtime_context else None
        previous = snapshot.text if snapshot else None
        from researchx.services.execution.tool_execution import recover_messages

        self._messages = await recover_messages(
            (list(messages)), self._tool_metadata, self._cwd, self._execution_session_id
        )
        latest = next((m.runtime_context for m in reversed(messages) if m.runtime_context), None)
        if previous and latest != previous:
            self._messages.append(
                ConversationMessage(
                    role="user",
                    context_origin="runtime",
                    runtime_context=previous,
                    runtime_context_manifest=snapshot.manifest if snapshot else None,
                )
            )
        if self._runtime_context is None:
            restored = next((m for m in reversed(messages) if m.runtime_context), None)
            self._runtime_context = restored.runtime_context if restored else None
            self._runtime_manifest = restored.runtime_context_manifest if restored else None

    def has_pending_continuation(self) -> bool:
        """Return True when the conversation ends with tool results awaiting a follow-up model turn."""
        # A state update may follow tool results when cancellation occurs just
        # before the next model response. Hidden context is not a new user turn.
        messages = [
            message for message in self._messages if message.content or not message.runtime_context
        ]
        if not messages:
            return False
        last = messages[-1]
        if last.role != "user":
            return False
        if not any(isinstance(block, ToolResultBlock) for block in last.content):
            return False
        for msg in reversed(messages[:-1]):
            if msg.role != "assistant":
                continue
            return bool(msg.tool_uses)
        return False

    async def submit_message(
        self, prompt: str | ConversationMessage
    ) -> AsyncGenerator[StreamEvent, None]:
        from researchx.api.retry import bind_api_audit
        from contextlib import aclosing

        session = self._tool_metadata.get("session_id") or self._execution_session_id
        with bind_api_audit(str(self._cwd), session):
            async with aclosing(self._submit_message(prompt)) as stream:
                async for event in stream:
                    yield event

    async def _submit_message(
        self, prompt: str | ConversationMessage
    ) -> AsyncGenerator[StreamEvent, None]:
        """Append a user message and execute the query loop."""
        user_message = (
            prompt
            if isinstance(prompt, ConversationMessage)
            else ConversationMessage.from_user_text(prompt)
        )
        message_id = self._tool_metadata.get("current_user_message_id")
        if message_id:
            user_message = user_message.model_copy(update={"message_id": message_id})
        store = self._tool_metadata.get("research_store")
        if store is not None and user_message.text.strip():
            from researchx.state.models import new_id
            from researchx.research_application import ResearchApplication

            selected = self._tool_metadata.pop("business_profile", None)
            self._tool_metadata.pop("current_business_profile", None)
            admission = await ResearchApplication(store).prepare_user_turn(
                user_message.text,
                selected,
                self._ask_user_prompt,
            )
            if not admission.allowed:
                yield ErrorEvent(message=admission.message)
                return
            if admission.profile:
                self._tool_metadata["current_business_profile"] = admission.profile
            window = json.loads(admission.message)
            self._tool_metadata["business_window"] = {
                "start": window["window_start"],
                "end": window["as_of"],
            }

            origin = user_message.message_id or new_id("msg")
            user_message = user_message.model_copy(update={"message_id": origin})
            source = await store.capture(
                origin_id=origin,
                content=user_message.text,
                kind="user",
                title="用户提供资料",
                locator=f"message:{origin}",
                request_profile=(
                    selected
                    if selected in {"event_commentary", "financial_commentary"}
                    else None
                ),
            )
            self._tool_metadata["latest_user_source_id"] = source.id
        # Retain the submitted prompt even if its recall is cancelled before the model starts.
        self._messages = sanitize_conversation_messages(self._messages)
        from uuid import uuid4

        user_message = user_message.model_copy(
            update={
                "context_origin": "user_input",
                "message_id": user_message.message_id or f"msg_{uuid4().hex}",
            }
        )
        self._messages.append(user_message)
        try:
            snapshot = await self._current_runtime_snapshot()
        except ValueError as exc:
            yield ErrorEvent(message=str(exc))
            return
        user_message = user_message.model_copy(
            update={
                "runtime_context": snapshot.text or None,
                "runtime_context_manifest": snapshot.manifest or None,
            }
        )
        self._messages[-1] = user_message
        if self._hook_executor is not None:
            await self._hook_executor.execute(
                HookEvent.USER_PROMPT_SUBMIT,
                {
                    "event": HookEvent.USER_PROMPT_SUBMIT.value,
                    "prompt": user_message.text,
                },
            )
        from researchx.permissions.capabilities import CapabilityContext

        context = QueryContext(
            trusted_settings=self._settings.model_copy(deep=True) if self._settings else None,
            capabilities=CapabilityContext(
                allow_trusted_host=bool(
                    self._settings
                    and self._settings.sandbox.allow_trusted_host
                    and not self._settings.sandbox.enabled
                )
            ),
            execution_session_id=self._execution_session_id,
            api_client=self._api_client,
            tool_registry=self._tool_registry,
            permission_checker=self._permission_checker,
            cwd=self._cwd,
            model=self._model,
            system_prompt=self._system_prompt,
            max_tokens=self._max_tokens,
            effort=self._effort,
            context_window_tokens=self._context_window_tokens,
            auto_compact_threshold_tokens=self._auto_compact_threshold_tokens,
            max_turns=self._max_turns,
            permission_prompt=self._permission_prompt,
            ask_user_prompt=self._ask_user_prompt,
            hook_executor=self._hook_executor,
            tool_metadata=self._tool_metadata,
            runtime_context_provider=self._current_runtime_context,
            runtime_snapshot_provider=self._current_runtime_snapshot,
            context_components=self._settings.context_components if self._settings else None,
            current_user_message_id=user_message.message_id,
            research_memory_enabled=self._settings.research.context.auto_inject
            if self._settings
            else True,
        )
        stream = self._run_context(context, (list(self._messages)))
        try:
            async for event in stream:
                yield event
        finally:
            await stream.aclose()

    async def _run_context(
        self, context: QueryContext, query_messages: list[ConversationMessage]
    ) -> AsyncGenerator[StreamEvent, None]:
        """Apply identical accounting, citations and cancellation to new and resumed loops."""
        store = self._tool_metadata.get("research_store")
        stream = cast(AsyncGenerator[Any, None], (run_query(context, query_messages)))
        try:
            async for event, usage in stream:
                if isinstance(event, AssistantTurnComplete):
                    if store is not None and not event.message.tool_uses:
                        from researchx.state.models import new_id

                        warning = await store.completion_warning()
                        text = event.message.text + (f"\n\n{warning}" if warning else "")
                        from researchx.state.deliveries import DeliveryService

                        user_request = next(
                            (
                                item.text
                                for item in reversed(query_messages)
                                if item.role == "user"
                                and item.text
                                and item.context_origin in {"user_input", None}
                            ),
                            "",
                        )
                        from researchx.state.business_coverage import answer_profiles, evaluate_coverage

                        profile, freeze_profile = answer_profiles(
                            user_request, self._tool_metadata.get("current_business_profile")
                        )
                        if profile is not None:
                            from researchx.state.store import CITATION_PATTERN

                            memory = await store.load()
                            cited = set(CITATION_PATTERN.findall(text))
                            coverage = evaluate_coverage(
                                memory.model_copy(
                                    update={
                                        "evidence_pool": {
                                            key: value
                                            for key, value in memory.evidence_pool.items()
                                            if key in cited
                                        }
                                    }
                                ),
                                profile,
                                user_request,
                            )
                            text = "【内部研究草稿 · 未经人工复核】\n\n" + text
                            if coverage.gaps:
                                text += "\n\n资料覆盖缺口：\n" + "\n".join(
                                    f"- {gap}" for gap in coverage.gaps
                                )
                        rendered, answer = await store.render_answer(text, new_id("answer"))
                        await DeliveryService(store).freeze_answer(
                            str(answer["answer_id"]),
                            user_request,
                            profile=freeze_profile,
                        )
                        updated = event.message.model_copy(
                            update={
                                "content": [TextBlock(text=rendered)],
                                "research_citations": answer,
                            }
                        )
                        query_messages[-1] = updated
                        event = AssistantTurnComplete(message=updated, usage=event.usage)
                    self._messages = list(query_messages)
                if usage is not None:
                    self._cost_tracker.add(usage)
                yield event
        except asyncio.CancelledError:
            runtime = self._tool_metadata.get("research_runtime")
            if runtime:
                memory = await runtime.store.load()
                if memory.project and memory.project.status not in {
                    "suspended",
                    "replanning",
                    "completed",
                    "failed",
                    "cancelled",
                }:
                    (await runtime.repository.suspend("Execution interrupted"))
            raise
        finally:
            await stream.aclose()
            from researchx.services.execution.tool_execution import recover_messages

            query_messages[:] = await recover_messages(
                query_messages, self._tool_metadata, self._cwd, self._execution_session_id
            )
            if store is not None:
                (await self._complete_interrupted_research_tools(query_messages, store))
            self._messages = list(query_messages)

    @staticmethod
    async def _complete_interrupted_research_tools(
        messages: list[ConversationMessage], store: ResearchStore
    ) -> None:
        """Preserve pending tool calls with explicit results across a cancellation boundary."""
        if not messages or not messages[-1].tool_uses:
            return
        memory = await store.load()
        results = []
        for call in messages[-1].tool_uses:
            sources = [source for source in memory.sources.values() if source.origin_id == call.id]
            if sources:
                source_ids = [source.id for source in sources]
                results.append(
                    ToolResultBlock(
                        tool_use_id=call.id,
                        content=f"Execution ended before its result was attached. Retained research sources: {source_ids}; read them before continuing.",
                        is_error=all(source.is_error for source in sources),
                        result_metadata={"research_sources": source_ids},
                    )
                )
            else:
                results.append(
                    ToolResultBlock(
                        tool_use_id=call.id,
                        content="Execution interrupted. Inspect current research state before retrying; an operation may already have committed.",
                        is_error=True,
                    )
                )
        messages.append(ConversationMessage(role="user", content=[block for block in results]))

    async def continue_pending(
        self, *, max_turns: int | None = None
    ) -> AsyncGenerator[StreamEvent, None]:
        from researchx.api.retry import bind_api_audit
        from contextlib import aclosing

        session = self._tool_metadata.get("session_id") or self._execution_session_id
        with bind_api_audit(str(self._cwd), session):
            async with aclosing(self._continue_pending(max_turns=max_turns)) as stream:
                async for event in stream:
                    yield event

    async def _continue_pending(
        self, *, max_turns: int | None = None
    ) -> AsyncGenerator[StreamEvent, None]:
        """Continue an interrupted tool loop without appending a new user message."""
        self._messages = sanitize_conversation_messages(self._messages)
        from researchx.permissions.capabilities import CapabilityContext

        context = QueryContext(
            trusted_settings=self._settings.model_copy(deep=True) if self._settings else None,
            capabilities=CapabilityContext(
                allow_trusted_host=bool(
                    self._settings
                    and self._settings.sandbox.allow_trusted_host
                    and not self._settings.sandbox.enabled
                )
            ),
            execution_session_id=self._execution_session_id,
            current_user_message_id=next(
                (
                    m.message_id
                    for m in reversed(self._messages)
                    if m.context_origin == "user_input"
                ),
                None,
            ),
            api_client=self._api_client,
            tool_registry=self._tool_registry,
            permission_checker=self._permission_checker,
            cwd=self._cwd,
            model=self._model,
            system_prompt=self._system_prompt,
            max_tokens=self._max_tokens,
            effort=self._effort,
            context_window_tokens=self._context_window_tokens,
            auto_compact_threshold_tokens=self._auto_compact_threshold_tokens,
            max_turns=max_turns if max_turns is not None else self._max_turns,
            permission_prompt=self._permission_prompt,
            ask_user_prompt=self._ask_user_prompt,
            hook_executor=self._hook_executor,
            tool_metadata=self._tool_metadata,
            runtime_context_provider=self._current_runtime_context,
            runtime_snapshot_provider=self._current_runtime_snapshot,
            context_components=self._settings.context_components if self._settings else None,
            research_memory_enabled=self._settings.research.context.auto_inject
            if self._settings
            else True,
        )
        stream = self._run_context(context, (list(self._messages)))
        try:
            async for event in stream:
                yield event
        finally:
            await stream.aclose()
