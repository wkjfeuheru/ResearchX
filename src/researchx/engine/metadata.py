"""Typed host-owned execution context. Persisted JSON contains only a safe subset."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict, Callable, Awaitable
import asyncio
from collections.abc import Mapping
from pathlib import Path

if TYPE_CHECKING:
    from researchx.api.usage import UsageSnapshot
    from researchx.config import Settings
    from researchx.engine.query import QueryContext
    from researchx.engine.observer import Observer
    from researchx.state.store import ResearchStore
    from researchx.state.runtime import ResearchAgentRuntime
    from researchx.mcp.client import McpClientManager
    from researchx.tools.base import ToolRegistry
    from researchx.state.business_coverage import Profile
    from researchx.storage.turns import TurnJournal


class ExecutionLease(TypedDict):
    id: str
    task_id: str
    task_revision: int
    plan_revision: int
    objective_revision: int
    epoch: int
    lease_id: str


class ExecutionMetadata(TypedDict, total=False):
    checkpoint_writer: TurnJournal
    current_user_message_id: str
    operation_mappings: dict[str, dict[str, object]]
    research_store: ResearchStore
    research_runtime: ResearchAgentRuntime
    research_workspace_runtime: ResearchAgentRuntime
    research_workspace_root: str | Path | None
    research_injection_budget: int
    research_auto_inject: bool
    memory_auto_inject_max_chars: int
    auto_summary_max_tokens: int
    auto_index_max_items: int
    query_default_limit: int
    query_max_limit: int
    conflict_max_turns: int
    conflict_timeout_seconds: float
    conflict_investigator: bool
    planning_child: bool
    planning_max_calls: int
    planning_timeout_seconds: float
    planning_token_budget: int
    planning_tokens: int
    planning_baseline: dict[str, int]
    research_execution: ExecutionLease
    subagent_child: bool
    subagent_baseline: tuple[int, int, int]
    subagent_parent_workspace: Path
    subagent_output_dir: Path
    subagent_max_calls: int
    subagent_max_concurrency: int
    subagent_timeout_seconds: float
    subagent_semaphore: asyncio.Semaphore
    http_semaphore: asyncio.Semaphore
    tool_max_concurrency: int
    tool_progress: Callable[[dict[str, object]], Awaitable[None]]
    subagent_scope: Mapping[str, object]
    parent_operation_id: str
    record_candidate_sources: Callable[[list[dict[str, object]]], Awaitable[None]]
    query_context: QueryContext
    tool_registry: ToolRegistry
    trusted_settings: Settings
    runtime_id: str
    session_id: str
    mcp_manager: McpClientManager
    account_subagent_usage: Callable[[UsageSnapshot], None]
    ask_user_prompt: Callable[[str], Awaitable[str]] | None
    edit_approval_prompt: Callable[[str, str, int, int], Awaitable[str]] | None
    vision_model_config: dict[str, str]
    image_generation_config: dict[str, str]
    extra_skill_dirs: tuple[str, ...]
    observer: Observer | None
    permission_mode: str
    session_approvals: dict[str, list[str]]
    invoked_skills: list[str]
    skill_settings: Settings
    selected_skill_roots: dict[str, str]
    context_budget: dict[str, object]
    latest_user_source_id: str
    business_profile: Profile
    current_business_profile: Profile
    business_window: dict[str, str]
    compact_last: dict[str, object]
    compact_checkpoints: list[dict[str, object]]
