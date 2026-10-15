"""Single tool execution boundary used by the existing handwritten loop."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import math
from sqlalchemy.exc import DBAPIError, TimeoutError as PoolTimeoutError
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4
from dataclasses import replace

from researchx.engine.messages import ToolResultBlock
from researchx.permissions.modes import PermissionMode
from researchx.hooks import HookEvent
from researchx.hooks.safety import redact
from researchx.tools.base import ToolExecutionContext, ToolResult
from researchx.tools.contracts import resolve_contract
from researchx.state.source_specs import SourceSpec
from researchx.state.source_policy import collected_usage, may_enter_external_model
from researchx.services.execution.operations import OperationStore
from researchx.storage.content import persist_object, read_object
from researchx.storage.database import current_database
from researchx.config.paths import get_data_dir

if TYPE_CHECKING:
    from researchx.engine.query import QueryContext

log = logging.getLogger(__name__)


async def _execute_impl(
    service: ToolExecutionService,
    context: QueryContext,
    tool_name: str,
    tool_use_id: str,
    tool_input: dict[str, object],
) -> ToolResultBlock:
    from researchx.engine.query import (
        _resolve_permission_file_path,
        _extract_permission_command,
        _offload_tool_output_if_needed,
    )

    store = (context.tool_metadata or {}).get("research_store")
    runtime = (context.tool_metadata or {}).get("research_runtime")
    initial_memory = await store.load() if store is not None else None
    project_mode = initial_memory is not None and initial_memory.project is not None
    if project_mode:
        assert store is not None
        if runtime is None:
            from researchx.state.runtime import ResearchAgentRuntime

            runtime = ResearchAgentRuntime(
                store,
                permission_checker=context.permission_checker,
                workspace_root=(context.tool_metadata or {}).get("research_workspace_root"),
                memory_auto_inject_max_chars=(context.tool_metadata or {}).get(
                    "memory_auto_inject_max_chars", 12000
                ),
            )
    log.debug("tool_call start: %s id=%s", tool_name, tool_use_id)

    tool = context.tool_registry.get(tool_name)
    if tool is None:
        log.warning("unknown tool: %s", tool_name)
        return ToolResultBlock(
            tool_use_id=tool_use_id,
            content=f"Unknown tool: {tool_name}",
            is_error=True,
            result_metadata={"status": "failed", "error_code": "unknown_tool"},
        )

    try:
        parsed_input = tool.parse_input(tool_input)
    except Exception as exc:
        log.warning("invalid input for %s: %s", tool_name, tool.validation_error_message(exc))
        return ToolResultBlock(
            tool_use_id=tool_use_id,
            content=tool.validation_error_message(exc),
            is_error=True,
            result_metadata={
                "status": "failed",
                "error_code": getattr(tool, "validation_error_code", "invalid_arguments"),
            },
        )

    service.contract = resolve_contract(tool, parsed_input)
    if not context.capabilities.permits(service.contract.required_capabilities):
        return ToolResultBlock(
            tool_use_id=tool_use_id,
            is_error=True,
            content="Required tool capabilities are not granted.",
            result_metadata={"status": "denied", "error_code": "capability_denied"},
        )

    # Normalize common tool inputs before permission checks so path rules apply
    # consistently across built-in tools that use `file_path`, `path`, or
    # directory-scoped roots such as `glob`/`grep`.
    tool_cwd = context.cwd
    if project_mode:
        assert runtime is not None and store is not None and initial_memory is not None
        # No admission await has occurred since this read. Keep one view for
        # initial validation/path resolution; post-approval checks still reload.
        tool_cwd = runtime._workspace_path(runtime.repository._project(initial_memory))
    _file_path = _resolve_permission_file_path(tool_cwd, tool_input, parsed_input)
    if _file_path and (project_mode or (context.tool_metadata or {}).get("subagent_child")):
        boundary = ToolExecutionContext(
            cwd=tool_cwd,
            metadata={
                **(context.tool_metadata or {}),
                **({"research_runtime": runtime} if runtime else {}),
            },
        )
        try:
            raw_path = next(
                (
                    getattr(parsed_input, key)
                    for key in ("file_path", "path", "root", "image_path")
                    if isinstance(getattr(parsed_input, key, None), str)
                    and getattr(parsed_input, key)
                ),
                _file_path,
            )
            (await boundary.resolve_path(raw_path, write=not tool.is_read_only(parsed_input)))
        except (ValueError, OSError, RuntimeError) as exc:
            return ToolResultBlock(
                tool_use_id=tool_use_id,
                is_error=True,
                content=str(exc),
                result_metadata={"status": "denied", "error_code": "workspace_boundary"},
            )
    child_scope = (context.tool_metadata or {}).get("subagent_scope")
    child_runtime = (context.tool_metadata or {}).get("research_workspace_runtime")
    if child_scope and child_runtime:
        from researchx.state.errors import ResearchError

        try:
            await child_runtime.validate_dispatch_scope(child_scope)
        except ResearchError as exc:
            return ToolResultBlock(
                tool_use_id=tool_use_id,
                content=str(exc),
                is_error=True,
                result_metadata={"status": "denied", "error_code": "parent_scope_changed"},
            )
    _command = _extract_permission_command(tool_input, parsed_input)
    log.debug(
        "permission check: %s read_only=%s path=%s cmd=%s",
        tool_name,
        tool.is_read_only(parsed_input),
        _file_path,
        _command and _command[:80],
    )
    decision = context.permission_checker.evaluate(
        tool_name,
        is_read_only=_permission_read_only(tool, parsed_input, service.contract, context),
        file_path=_file_path,
        command=_command,
    )
    if not decision.allowed:
        if decision.requires_confirmation and context.permission_prompt is not None:
            log.debug("permission prompt for %s: %s", tool_name, decision.reason)
            if context.hook_executor is not None:
                await context.hook_executor.execute(
                    HookEvent.NOTIFICATION,
                    {
                        "event": HookEvent.NOTIFICATION.value,
                        "notification_type": "permission_prompt",
                        "tool_name": tool_name,
                        "reason": decision.reason,
                    },
                )
            confirmed = await context.permission_prompt(tool_name, decision.reason)
            if not confirmed:
                log.debug("permission denied by user for %s", tool_name)
                return ToolResultBlock(
                    tool_use_id=tool_use_id,
                    content=decision.reason or f"Permission denied for {tool_name}",
                    is_error=True,
                    result_metadata={
                        "outcome": "denied",
                        "status": "denied",
                        "error_code": "permission_denied",
                    },
                )
        else:
            log.debug("permission blocked for %s: %s", tool_name, decision.reason)
            return ToolResultBlock(
                tool_use_id=tool_use_id,
                content=decision.reason or f"Permission denied for {tool_name}",
                is_error=True,
                result_metadata={
                    "outcome": "denied",
                    "status": "denied",
                    "error_code": "permission_denied",
                },
            )

    if context.hook_executor is not None:
        settings = (context.tool_metadata or {}).get("trusted_settings")
        owner = None
        if project_mode:
            from researchx.config import Settings
            from researchx.sandbox.policy import ExecutionOwner, report_settings

            assert runtime is not None and store is not None
            project = runtime.repository._project((await store.load()))
            settings = report_settings(settings or Settings(), tool_cwd)
            owner = ExecutionOwner(
                runtime_id=(context.tool_metadata or {}).get("runtime_id")
                or context.execution_session_id,
                project_id=project.id,
                execution_id=tool_use_id,
                workspace=tool_cwd,
            )
        context = replace(
            context,
            hook_executor=context.hook_executor.scoped(
                cwd=tool_cwd,
                settings=settings,
                checker=context.permission_checker,
                capabilities=context.capabilities,
                prompt=context.permission_prompt,
                owner=owner,
            ),
        )
    replay = await service.begin(context, tool, parsed_input, tool_use_id, tool_cwd)
    if replay is not None:
        return replay
    if store is not None:
        from researchx.research_application import ResearchApplication

        async def guard(name: str) -> str | None:
            if not project_mode or runtime is None:
                return None
            return await runtime.guard_tool(name, tool_input)

        admission = await ResearchApplication(store).admit_tool(
            tool_name, project_mode=project_mode, guard=guard
        )
        if not admission.allowed:
            return ToolResultBlock(tool_use_id=tool_use_id, content=admission.message, is_error=True)
    if tool_name == "dispatch_subagents" and store is not None:
        from researchx.state.runtime import ResearchAgentRuntime
        from researchx.state.errors import ResearchError

        try:
            ResearchAgentRuntime.dispatch_scope(await store.load())
        except ResearchError as exc:
            return ToolResultBlock(
                tool_use_id=tool_use_id,
                content=str(exc),
                is_error=True,
                result_metadata={"status": "denied", "error_code": "research_task_required"},
            )
    if context.hook_executor is not None:
        service.started_hooks = context.hook_executor.has_effects(HookEvent.PRE_TOOL_USE, tool_name)
        pre_hooks = await context.hook_executor.execute(
            HookEvent.PRE_TOOL_USE,
            {
                "tool_name": tool_name,
                "tool_input": tool_input,
                "event": HookEvent.PRE_TOOL_USE.value,
            },
        )
        if pre_hooks.blocked:
            return ToolResultBlock(
                tool_use_id=tool_use_id,
                content=pre_hooks.reason or f"pre_tool_use hook blocked {tool_name}",
                is_error=True,
                result_metadata={
                    "status": "partial" if service.started_hooks else "denied",
                    "error_code": "pre_hook_blocked",
                },
            )

    log.debug("executing %s ...", tool_name)
    t0 = time.monotonic()
    execution = None
    baseline = None
    if project_mode:
        assert runtime is not None and store is not None
        from researchx.state.runtime import CONTROL_TOOLS, PLANNING_TOOLS
        from researchx.state.errors import ResearchError

        try:
            (await runtime.resolve_workspace(runtime.repository._project((await store.load())).id))
            if tool_name in PLANNING_TOOLS:
                baseline = await runtime.repository.reserve_planning(tool_name)
            elif (
                tool_name not in CONTROL_TOOLS
                and (await store.load()).research_state.current_task_id
            ):
                execution = await runtime.repository.begin_execution(tool_name, tool_use_id)
        except ResearchError as exc:
            return ToolResultBlock(tool_use_id=tool_use_id, content=str(exc), is_error=True)
    try:
        result = await service.invoke(
            tool,
            parsed_input,
            ToolExecutionContext(
                cwd=tool_cwd,
                capabilities=context.capabilities.restrict(service.contract.required_capabilities),
                settings=context.trusted_settings.model_copy(deep=True)
                if context.trusted_settings
                else None,
                runtime_id=(context.tool_metadata or {}).get("runtime_id"),
                metadata={
                    "tool_registry": context.tool_registry,
                    "ask_user_prompt": context.ask_user_prompt,
                    **(context.tool_metadata or {}),
                    "query_context": context,
                    **({"research_runtime": runtime} if runtime else {}),
                    **({"planning_baseline": baseline} if baseline else {}),
                    **({"research_execution": execution} if execution else {}),
                },
                hook_executor=context.hook_executor,
            ),
        )
    except BaseException:
        if execution:
            assert runtime is not None
            (await runtime.repository.cancel_execution(execution["id"]))
        if baseline:
            assert runtime is not None
            (await runtime.repository.suspend("Planning execution interrupted"))
        raise
    if baseline:
        assert runtime is not None
        result = await runtime.commit_planning_result(tool_name, result, baseline)
    if store is not None and tool_name == "bash" and "returncode" in result.metadata:
        # A shell can print any workspace file. Execution permission is not a
        # source licence; consume registered, authorised artifacts instead of
        # forwarding opaque stdout/stderr to an external model.
        result = replace(
            result,
            output=(
                f"Shell 已结束，退出码：{result.metadata['returncode']}。"
                "原始输出的来源使用范围未经确认，未送入模型。请读取已登记且获准使用的计算产物。"
            ),
            metadata={**result.metadata, "source_use_status": "unclassified_output_withheld"},
        )
    elapsed = time.monotonic() - t0
    audit_store = store
    parent_runtime = (context.tool_metadata or {}).get("research_workspace_runtime")
    if audit_store is None and parent_runtime is not None:
        audit_store = parent_runtime.store
    if audit_store is not None and tool_name in {"web_search", "web_fetch"}:
        from researchx.storage.schema import collection_attempts
        from sqlalchemy.dialects.postgresql import insert

        classification = result.metadata.get("source_classification", {})
        categories = (
            classification.get("categories", []) if isinstance(classification, dict) else []
        )
        channel = (
            str(result.metadata.get("search_category") or "all")
            if tool_name == "web_search"
            else str(categories[0])
            if categories
            else "unclassified"
        )
        window = (context.tool_metadata or {}).get("business_window", {})
        async with audit_store.transaction() as db:
            await db.execute(
                insert(collection_attempts)
                .values(
                    workspace_id=audit_store.workspace_id,
                    session_id=audit_store.session_id,
                    attempt_id=hashlib.sha256(
                        f"{context.execution_session_id}:{tool_use_id}".encode()
                    ).hexdigest(),
                    channel=channel,
                    scope=str(result.metadata.get("search_scope") or tool_name),
                    query_hash=hashlib.sha256(
                        json.dumps(tool_input, sort_keys=True, ensure_ascii=False).encode()
                    ).hexdigest(),
                    outcome=str(
                        result.metadata.get("outcome")
                        or ("error" if result.is_error else "success")
                    ),
                    errors=list(
                        result.metadata.get("error_codes")
                        or ([result.error_code or "tool_error"] if result.is_error else [])
                    ),
                    window_start=window.get("start"),
                    window_end=window.get("end"),
                    created_at=time.time(),
                )
                .on_conflict_do_nothing()
            )
    operation = tool_input.get("operation") if tool_name == "research_memory" else None
    if (
        not result.is_error
        and isinstance(operation, dict)
        and operation.get("action") == "read"
        and operation.get("include_content")
        and store is not None
    ):
        memory = await store.load()
        source_ids = operation.get("ids", [])
        if isinstance(source_ids, list) and any(
            key in memory.sources and not may_enter_external_model(memory.sources[key].usage)
            for key in source_ids
            if isinstance(key, str)
        ):
            result = replace(
                result,
                output="来源授权尚未确认，不能向当前模型读取原文。",
                is_error=True,
                error_code="source_use_not_authorized",
            )
    specs_to_validate = result.metadata.get("research_source_specs")
    if specs_to_validate and any(
        not isinstance(spec, dict)
        or not isinstance(spec.get("content"), str)
        or spec.get("kind", "tool")
        not in {"user", "tool", "web", "file", "search", "mcp", "calculation"}
        for spec in specs_to_validate
    ):
        if execution:
            assert runtime is not None
            await runtime.repository.cancel_execution(execution["id"], "Invalid source contract")
        return ToolResultBlock(
            tool_use_id=tool_use_id,
            content="Tool result rejected: invalid provenance or schema",
            is_error=True,
            result_metadata={
                "status": "blocked",
                "error_code": "invalid_provenance",
                **({"execution_id": execution["id"]} if execution else {}),
            },
        )
    if (
        (store is not None or (context.tool_metadata or {}).get("subagent_child"))
        and not result.is_error
        and result.metadata.get("research_source_specs")
    ):
        unapproved = [
            spec
            for spec in result.metadata.get("research_source_specs", [])
            if not may_enter_external_model(collected_usage(tool_name, spec))
        ]
        if unapproved:
            result = replace(
                result,
                output="资料使用范围尚未确认；当前模型不能接收此来源内容。请先完成授权分类。",
                is_error=True,
                error_code="source_use_not_authorized",
                metadata={
                    key: value
                    for key, value in result.metadata.items()
                    if key != "research_source_specs"
                },
            )
    candidate_capture = (context.tool_metadata or {}).get("record_candidate_sources")
    if candidate_capture and not result.is_error:
        specs = result.metadata.get("research_source_specs")
        if specs:
            await candidate_capture(
                [
                    {
                        **spec,
                        "__host_usage": collected_usage(tool_name, spec).model_dump(mode="json"),
                    }
                    for spec in cast(list[dict[str, object]], specs)
                ]
            )
            result = replace(
                result,
                metadata={
                    key: value
                    for key, value in result.metadata.items()
                    if key != "research_source_specs"
                },
            )
    if store is not None and (
        execution
        or tool_name
        not in {
            "research_memory",
            "planner",
            "replanner",
            "research_project",
        }
    ):
        source_specs: list[SourceSpec] | None = result.metadata.get("research_source_specs")
        if source_specs is None:
            source_specs = [
                {
                    "content": result.output,
                    "kind": "calculation"
                    if tool_name == "bash"
                    else "user"
                    if tool_name == "ask_user_question"
                    else "tool",
                    "title": tool_name,
                    "locator": f"tool:{tool_name}:{tool_use_id}",
                    "fragment": True,
                }
            ]
        sources: list[str] = []
        if execution:
            assert runtime is not None
            from researchx.state.errors import ResearchError

            rejection = "Late tool result discarded: execution/plan/task revision was revoked"
            try:
                receipt = await runtime.repository.commit_execution(
                    execution["id"], source_specs, is_error=result.is_error
                )
            except (ResearchError, ValueError, KeyError, TypeError) as exc:
                rejection = (
                    f"Tool result rejected: invalid provenance or schema ({type(exc).__name__})"
                )
                (
                    await runtime.repository.cancel_execution(
                        execution["id"], f"Invalid tool result: {exc}"
                    )
                )
                receipt = {
                    "accepted": False,
                    "execution_id": execution["id"],
                    "research_sources": [],
                    "task_revision": execution["task_revision"],
                    "plan_revision": execution["plan_revision"],
                }
            sources = cast(list[str], receipt["research_sources"])

            result = replace(result, metadata={**result.metadata, **receipt})
            if not receipt["accepted"]:
                result = replace(result, output=rejection, is_error=True)
        else:
            async with store.transaction():
                dispatch_scope = (
                    result.metadata.get("dispatch_scope")
                    if tool_name == "dispatch_subagents"
                    else None
                )
                if dispatch_scope and runtime:
                    from researchx.state.errors import ResearchError

                    try:
                        await runtime.validate_dispatch_scope(dispatch_scope)
                    except ResearchError as exc:
                        source_specs = []
                        result = replace(
                            result,
                            output=str(exc),
                            is_error=True,
                            error_code="parent_scope_changed",
                        )
                for index, spec in enumerate(source_specs):
                    sources.append(
                        (
                            await store.capture(
                                origin_id=tool_use_id,
                                index=index,
                                content=spec["content"],
                                kind=spec.get("kind", "tool"),
                                title=spec.get("title", tool_name),
                                locator=spec.get("locator", f"tool:{tool_name}:{tool_use_id}"),
                                fragment=spec.get("fragment", False),
                                published_at=spec.get("published_at"),
                                event_at=spec.get("event_at"),
                                is_error=result.is_error,
                                usage=collected_usage(tool_name, spec),
                                original_locator=spec.get("original_locator"),
                                corrects_source_id=spec.get("corrects_source_id"),
                            )
                        ).id
                    )

        result = replace(
            result,
            metadata={
                **{
                    key: value
                    for key, value in result.metadata.items()
                    if key != "research_source_specs"
                },
                "research_sources": sources,
            },
        )
    result = await service.bound_output(result)
    log.debug(
        "executed %s in %.2fs err=%s output_len=%d",
        tool_name,
        elapsed,
        result.is_error,
        len(result.output or ""),
    )
    inline_output, artifact_path = await _offload_tool_output_if_needed(
        tool_name=tool_name,
        tool_use_id=tool_use_id,
        output=result.output,
        workspace=service.ledger.workspace,
    )
    tool_result = ToolResultBlock(
        tool_use_id=tool_use_id,
        content=inline_output,
        is_error=result.is_error,
        result_metadata={
            key: value
            for key, value in (result.metadata or {}).items()
            if key != "research_source_specs"
        },
    )
    if artifact_path is not None:
        tool_result.result_metadata["tool_output_artifact"] = str(artifact_path)
    if store is not None and result.metadata.get("research_sources"):
        tool_result.content += "\n\nresearch_sources: " + json.dumps(
            result.metadata["research_sources"]
        )
        tool_result.content += f"\nresearch_revision: {(await store.load()).revision}"
    if tool_name == "skill" and not tool_result.is_error:
        skill_name = str(tool_input.get("name", "")).strip()
        if skill_name and context.tool_metadata is not None:
            skills = context.tool_metadata.setdefault("invoked_skills", [])
            if skill_name not in skills:
                skills.append(skill_name)
    (await service.finish(tool_result))
    if context.hook_executor is not None:
        hook_output = tool_result.content
        if store is not None:
            policy_memory = await store.load()
            if any(
                key in policy_memory.sources
                and policy_memory.sources[key].kind != "user"
                and not may_enter_external_model(policy_memory.sources[key].usage)
                for key in tool_result.result_metadata.get("research_sources", [])
            ):
                hook_output = "来源使用范围尚未确认；不向 Hook 模型提供原始内容。"
        post = await context.hook_executor.execute(
            HookEvent.POST_TOOL_USE,
            {
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_output": hook_output,
                "tool_is_error": tool_result.is_error,
                "event": HookEvent.POST_TOOL_USE.value,
            },
        )
        failures = [item.hook_type for item in post.results if not item.success]
        if failures:
            tool_result.result_metadata["post_hook_failures"] = failures
    return tool_result


class ToolExecutionService:
    """One invocation adapter; state survives instances through the PostgreSQL ledger."""

    def __init__(self, *, claim_wait_seconds: float = 30.0) -> None:
        if not math.isfinite(claim_wait_seconds) or not 0 <= claim_wait_seconds <= 300:
            raise ValueError("claim_wait_seconds must be finite and within 0..300 seconds")
        self.claim_wait_seconds = claim_wait_seconds
        self.owner = uuid4().hex
        self.ledger: Any = None
        self.operation: Any = None
        self.contract: Any = None
        self.settled = False
        self.started_hooks = False
        self.started_body = False
        self.claimed = False

    async def execute(
        self, context: QueryContext, name: str, call: str, arguments: dict[str, object]
    ) -> ToolResultBlock:
        try:
            result = await _execute_impl(self, context, name, call, arguments)
            if self.claimed and not self.settled:
                (
                    await self.finish(
                        result, blocked=not self.started_body and not self.started_hooks
                    )
                )
            result.result_metadata.setdefault("status", "failed" if result.is_error else "success")
            if result.is_error:
                result.result_metadata.setdefault("error_code", "admission_rejected")
            return result
        except asyncio.CancelledError:
            if self.claimed and not self.settled:
                status = (
                    "uncertain"
                    if (
                        self.started_hooks
                        or (self.started_body and self.contract.effect != "read_only")
                    )
                    else "cancelled"
                )
                try:
                    (
                        await self.ledger.settle(
                            self.operation["operation_id"],
                            status,
                            owner=self.owner,
                            error_code="cancelled_after_start"
                            if self.started_body
                            else "cancelled",
                        )
                    )
                except Exception:
                    log.error(
                        "Cancellation receipt could not settle; operation requires host verification"
                    )
            raise
        except Exception as exc:
            from researchx.storage.turns import CheckpointError

            if isinstance(exc, CheckpointError):
                raise
            if (
                context.tool_metadata
                and context.tool_metadata.get("checkpoint_writer")
                and isinstance(exc, (DBAPIError, PoolTimeoutError))
            ):
                raise CheckpointError("操作账本保存失败；已停止后续执行") from None
            if not self.claimed and isinstance(exc, (DBAPIError, PoolTimeoutError)):
                sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
                busy = isinstance(exc, PoolTimeoutError) or sqlstate in {
                    "55P03",
                    "40001",
                    "40P01",
                    "57014",
                }
                return ToolResultBlock(
                    tool_use_id=call,
                    content="Operation database unavailable; no tool was executed.",
                    is_error=True,
                    result_metadata={
                        "status": "blocked",
                        "error_code": "resource_conflict" if busy else "database_unavailable",
                        "no_effect": True,
                    },
                )
            status = (
                "uncertain"
                if (
                    self.started_hooks
                    or (self.started_body and self.contract.effect != "read_only")
                )
                else "failed"
            )
            if self.settled:
                # A post-processing exception cannot erase an already durable success.
                result = await self.load_result(call)
                result.result_metadata["post_hook_failures"] = [type(exc).__name__]
                return result
            result = ToolResultBlock(
                tool_use_id=call,
                content=f"Tool execution {status}: {type(exc).__name__}: {(redact(str(exc)))}",
                is_error=True,
                result_metadata={
                    "status": status,
                    "error_code": "timeout"
                    if isinstance(exc, (TimeoutError, asyncio.TimeoutError))
                    else "execution_error",
                },
            )
            if self.claimed:
                (
                    await self.finish(
                        result, blocked=not self.started_body and not self.started_hooks
                    )
                )
            return result

    async def begin(
        self, context: QueryContext, tool: Any, arguments: Any, call: str, cwd: Path
    ) -> ToolResultBlock | None:
        # Retain the exact contract already admitted before any approval await.
        assert self.contract is not None
        metadata = context.tool_metadata or {}
        store = metadata.get("research_store")
        session = metadata.get("session_id") or (
            store.session_id if store else context.execution_session_id
        )
        scope = str(Path(cwd).resolve())
        root = store.directory if store else get_data_dir() / "executions"
        self.ledger = OperationStore(
            store.cwd if store else (context.execution_workspace or cwd), directory=root
        )

        # Unknown resources serialize. Explicit file resources participate in read/write conflicts.
        def resources(items: tuple[str, ...]) -> list[str]:
            return [
                str((Path(cwd) / Path(getattr(arguments, item)).expanduser()).resolve())
                if item in {"path", "image_path"} and getattr(arguments, item, None)
                else item
                for item in items
                if item != "image_path" or getattr(arguments, item, None)
            ]

        declared = {
            "read": resources(self.contract.resources_read),
            "write": resources(self.contract.resources_write),
        }
        if self.contract.parallelism == "serial":
            declared = {"read": [], "write": ["*"]}
        effect_hook = bool(
            context.hook_executor
            and context.hook_executor.has_effects(HookEvent.PRE_TOOL_USE, tool.name)
        )
        if effect_hook:
            # Hooks have no narrower resource declaration. A file tool's path
            # cannot stand in for arbitrary PRE command/HTTP/agent side effects.
            declared = {"read": [], "write": ["*"]}
        # Orchestration must not hold its children's resource lock while awaiting them.
        if metadata.get("subagent_child"):
            scope += ":child:" + str(metadata.get("subagent_output_dir", ""))
            if metadata.get("parent_operation_id"):
                declared["parent_operation"] = [metadata["parent_operation_id"]]
        (await self.ledger.recover(session=session, scope=scope))
        digest = hashlib.sha256(
            json.dumps(arguments.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
        run = hashlib.sha256(
            f"{session}:{context.current_user_message_id or context.execution_session_id}".encode()
        ).hexdigest()
        self.operation = await self.ledger.prepare(
            session=session,
            scope=scope,
            run=run,
            call=call,
            tool=tool.name,
            version=self.contract.version,
            digest=digest,
            effect="mixed" if effect_hook else self.contract.effect,
            resources=declared,
        )
        writer = metadata.get("checkpoint_writer")
        if writer is not None:
            checkpoint_scope = str(context.cwd.resolve())
            if metadata.get("subagent_child"):
                checkpoint_scope += ":child:" + str(metadata.get("subagent_output_dir", ""))
            await writer.record_operation(self.operation, scope=checkpoint_scope)
        operation_id = self.operation["operation_id"]
        deadline = time.monotonic() + self.claim_wait_seconds
        delay = 0.02
        while True:
            current = await self.ledger.get(operation_id, session=session, scope=scope)
            if current["status"] == "succeeded":
                self.operation = current
                self.settled = True
                return await self.load_result(call)
            if current["status"] not in {"prepared", "running"}:
                self.settled = True
                return ToolResultBlock(
                    tool_use_id=call,
                    is_error=True,
                    content=f"操作 {operation_id} 当前状态为 {current['status']}；需要核对或明确恢复，不能重复执行该副作用。"
                    "请用户使用 rx operations list 和 rx operations resolve 核验。",
                    result_metadata={
                        "status": current["status"],
                        "error_code": "recovery_required",
                        "operation_id": operation_id,
                        "workspace": self.ledger.cwd,
                        "session_id": session,
                    },
                )
            conflicts = await self.ledger.unresolved_conflicts(operation_id)
            if conflicts:
                # A transient resource denial must not poison this call identity:
                # keep it prepared so the same call can proceed after verification.
                self.settled = True
                return ToolResultBlock(
                    tool_use_id=call,
                    is_error=True,
                    content="未决副作用阻止本次写操作："
                    + ", ".join(conflicts)
                    + "。只读诊断仍可使用。请用户运行 rx operations list --cwd 工作区，"
                    "核对外部状态后使用 rx operations resolve；不要换调用 ID 重放写操作。",
                    result_metadata={
                        "status": "blocked",
                        "error_code": "reconciliation_required",
                        "operation_id": operation_id,
                        "conflicting_operations": conflicts,
                        "workspace": self.ledger.cwd,
                        "session_id": session,
                    },
                )
            attempt = await self.ledger.claim(operation_id, self.owner)
            if attempt:
                self.claimed = True
                self.operation = await self.ledger.get(operation_id, session=session, scope=scope)
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                code = (
                    "operation_in_progress"
                    if current["status"] == "running"
                    else "resource_conflict"
                )
                self.settled = True
                return ToolResultBlock(
                    tool_use_id=call,
                    is_error=True,
                    content=f"Admission wait expired: {code}. No tool or hook was executed.",
                    result_metadata={
                        "status": "blocked",
                        "error_code": code,
                        "operation_id": operation_id,
                    },
                )
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 1.5, 0.5)

    async def invoke(self, tool: Any, arguments: Any, context: ToolExecutionContext) -> Any:
        self.started_body = True
        # Stable across all attempts, never exposed as a model-controlled argument.
        context.operation_id = self.operation["operation_id"]
        context.idempotency_key = self.operation["idempotency_key"]
        remaining = self.contract.max_attempts - self.operation["attempts"] + 1
        for attempt in range(1, remaining + 1):
            execution = (
                (
                    tool.execute_with_idempotency_key(
                        arguments, context, idempotency_key=context.idempotency_key
                    )
                )
                if self.contract.retry_mode == "idempotency_key"
                else tool.execute(arguments, context)
            )
            result = await asyncio.wait_for(execution, self.contract.timeout_seconds)
            no_effect = (
                result.no_effect
                if result.no_effect is not None
                else result.metadata.get("no_effect") is True
            )
            result = replace(
                result,
                metadata={
                    **result.metadata,
                    "no_effect": no_effect,
                    "retryable": result.retryable or result.metadata.get("retryable") is True,
                },
            )
            if not result.is_error or attempt >= remaining or self.contract.retry_mode == "never":
                break
            if (
                not result.metadata["retryable"]
                or result.status in {"partial", "uncertain"}
                or result.error_code
                in {
                    "authentication",
                    "authorization",
                    "invalid_request",
                    "quota_exhausted",
                    "cancelled",
                    "context_length",
                }
                or result.metadata.get("error_code")
                in {
                    "authentication",
                    "authorization",
                    "invalid_request",
                    "quota_exhausted",
                    "cancelled",
                    "context_length",
                }
            ):
                break
            no_effect = (
                self.contract.effect == "read_only" or result.metadata.get("no_effect") is True
            )
            if self.contract.retry_mode == "reconcile_before_retry":
                no_effect = await asyncio.wait_for(
                    (tool.reconcile_no_effect(arguments, context)), self.contract.timeout_seconds
                )
                result = replace(result, metadata={**result.metadata, "no_effect": bool(no_effect)})
            if not no_effect:
                break
            (await self.ledger.next_attempt(context.operation_id, owner=self.owner))
            await asyncio.sleep(min(0.1 * 2 ** (attempt - 1), 1.0))
        if self.contract.output_model is not None and not result.is_error:
            self.contract.output_model.model_validate_json(result.output)
        status = result.status or ("failed" if result.is_error else "success")
        code = result.error_code or result.metadata.get("error_code")
        if (
            result.is_error
            and self.contract.effect in {"external_write", "unknown", "mixed"}
            and not result.metadata.get("no_effect")
        ):
            status = "uncertain"
        metadata = {
            **result.metadata,
            "status": status,
            "error_code": code,
            "operation_id": context.operation_id,
            "contract_version": self.contract.version,
        }
        return replace(result, is_error=status != "success", metadata=metadata)

    async def bound_output(self, result: ToolResult) -> ToolResult:
        """Bound model-visible output only after ResearchStore captured the full source."""
        if len(result.output) <= self.contract.max_output_chars:
            return result
        reference = await persist_object(
            self.ledger.database, self.ledger.workspace, result.output.encode()
        )
        return replace(
            result,
            output=result.output[: self.contract.max_output_chars]
            + f"\n[Full output object: {reference}]",
            metadata={**result.metadata, "tool_output_artifact": reference},
        )

    async def finish(self, result: ToolResultBlock, *, blocked: bool = False) -> None:
        if not self.operation or self.settled:
            return
        operation_id = self.operation["operation_id"]
        status = (
            "blocked"
            if blocked
            else result.result_metadata.get("status", "failed" if result.is_error else "success")
        )
        state = "succeeded" if status == "success" else status
        if state == "denied":
            state = "blocked"
        result.result_metadata.update(operation_id=operation_id, status=status)
        reference = await persist_object(
            self.ledger.database, self.ledger.workspace, result.model_dump_json().encode()
        )
        (
            await self.ledger.settle(
                operation_id,
                state,
                owner=self.owner,
                result_ref=reference,
                error_code=result.result_metadata.get("error_code"),
                external_request_id=result.result_metadata.get("external_request_id"),
            )
        )
        self.operation["result_ref"] = reference
        self.settled = True

    async def load_result(self, call: str) -> ToolResultBlock:
        result = await _receipt_result(self.operation, call)
        result.tool_use_id = call
        result.result_metadata["replayed_receipt"] = True
        return result


async def recover_messages(
    messages: list[Any], metadata: Any, cwd: Path, session_id: str
) -> list[Any]:
    """Attach durable outcomes to unmatched calls without replaying a write."""
    from researchx.engine.messages import ConversationMessage

    store = metadata.get("research_store")
    runtime = metadata.get("research_runtime")
    session = (metadata.get("session_id")) or (store.session_id if store else session_id)
    root = store.directory if store else get_data_dir() / "executions"
    if store and runtime and (await store.load()).project:
        cwd = runtime._workspace_path(runtime.repository._project((await store.load())))
    scope = str(cwd.resolve())
    if metadata.get("subagent_child"):
        scope += ":child:" + str(metadata.get("subagent_output_dir", ""))
    ledger = OperationStore(store.cwd if store else cwd, directory=root)
    (await ledger.recover(session=session, scope=scope))
    recovered = []
    skip = False
    for index, message in enumerate(messages):
        if skip:
            skip = False
            continue
        recovered.append(message)
        if not message.tool_uses:
            continue
        following = messages[index + 1] if index + 1 < len(messages) else None
        paired = (
            {
                result.tool_use_id
                for result in following.content
                if isinstance(result, ToolResultBlock)
            }
            if following and following.role == "user"
            else set()
        )
        results = []
        for call in message.tool_uses:
            if call.id in paired:
                continue
            from researchx.services.execution.operations import operation_identity

            mapping = metadata.get("operation_mappings", {}).get(call.id)
            call_scope = mapping["scope"] if mapping else scope
            identity = operation_identity(ledger.workspace, session, call_scope, call.id)
            if mapping and identity != mapping["operation_id"]:
                raise ValueError("Invalid operation recovery mapping")
            try:
                await ledger.recover(session=session, scope=call_scope)
                receipt = await ledger.get(identity, session=session, scope=call_scope)
                if mapping and (
                    receipt["input_digest"] != mapping["input_digest"]
                    or receipt["tool"] != call.name
                ):
                    raise ValueError("Operation receipt does not match checkpoint")
            except ValueError:
                results.append(
                    ToolResultBlock(
                        tool_use_id=call.id,
                        is_error=True,
                        content="No durable receipt exists. Inspect the operation before explicitly resubmitting.",
                    )
                )
                continue
            if receipt["status"] == "succeeded" and receipt["result_ref"]:
                result = await _receipt_result(receipt, call.id)
                result.result_metadata["replayed_receipt"] = True
            else:
                result = ToolResultBlock(
                    tool_use_id=call.id,
                    is_error=True,
                    content=f"Execution interrupted. Recovered operation {identity}: {receipt['status']}. Reconciliation is required before repeating side effects.",
                    result_metadata={
                        "operation_id": identity,
                        "status": receipt["status"],
                        "error_code": "recovery_required",
                    },
                )
            if store is not None:
                sources = [
                    source
                    for source in (await store.load()).sources.values()
                    if source.origin_id == call.id
                ]
                if sources:
                    source_ids = [source.id for source in sources]
                    result.content += (
                        f"\nRetained research sources: {source_ids}; read them before continuing."
                    )
                    result.result_metadata["research_sources"] = source_ids
                    if receipt["effect"] == "read_only" and receipt["status"] != "succeeded":
                        result.is_error = all(source.is_error for source in sources)
            results.append(result)
        if results:
            if following is not None and following.role == "user":
                existing = {
                    result.tool_use_id: result
                    for result in following.content
                    if isinstance(result, ToolResultBlock)
                }
                existing.update({result.tool_use_id: result for result in results})
                other = [
                    block for block in following.content if not isinstance(block, ToolResultBlock)
                ]
                recovered.append(
                    following.model_copy(
                        update={
                            "content": [existing[call.id] for call in message.tool_uses] + other
                        }
                    )
                )
                skip = True
            else:
                recovered.append(
                    ConversationMessage(role="user", content=[block for block in results])
                )
    return recovered


async def _receipt_result(receipt: dict[str, Any], call: str) -> ToolResultBlock:
    try:
        result = ToolResultBlock.model_validate_json(
            await read_object(current_database(), receipt["workspace_id"], receipt["result_ref"])
        ).model_copy(update={"tool_use_id": call})
        if (
            result.is_error
            or result.result_metadata.get("status") != "success"
            or result.result_metadata.get("operation_id") != receipt["operation_id"]
        ):
            raise ValueError("Saved result does not establish this operation's success")
        return result
    except (OSError, ValueError, TypeError):
        return ToolResultBlock(
            tool_use_id=call,
            is_error=True,
            content="Operation already succeeded but its saved result is unavailable. Restore the artifact; do not repeat the side effect.",
            result_metadata={
                "status": "blocked",
                "operation_status": "succeeded",
                "error_code": "artifact_unavailable",
                "operation_id": receipt["operation_id"],
            },
        )


def _permission_read_only(tool: Any, arguments: Any, contract: Any, context: QueryContext) -> bool:
    from researchx.tools.research_memory_tool import ResearchMemoryTool
    from researchx.tools.research_project_tool import ResearchProjectTool

    if contract.effect == "local_write":
        return (
            type(tool) in {ResearchMemoryTool, ResearchProjectTool}
            and context.capabilities.permits(contract.required_capabilities)
            and context.permission_checker.mode != PermissionMode.PLAN
        )
    return contract.effect in {"read_only", "model_call"} and tool.is_read_only(arguments)
