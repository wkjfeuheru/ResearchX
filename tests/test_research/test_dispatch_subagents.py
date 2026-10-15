"""Finite child execution, shared concurrency, authority and workspace boundaries."""

import asyncio
import json
from researchx.state.dispatch_audit import dispatch_summaries
from tests.postgres_helpers import source_path
from researchx.storage.conversations import ConversationRecords, subagent_session_id
from researchx.storage.database import current_database
from pathlib import Path

import pytest
from pydantic import ValidationError

from researchx.api.client import ApiMessageCompleteEvent
from researchx.api.usage import UsageSnapshot
from researchx.config.settings import PermissionSettings, ResearchMemorySettings
from researchx.engine.messages import (
    ConversationMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from researchx.engine.query import QueryContext, _execute_tool_call
from researchx.permissions.checker import PermissionChecker
from researchx.state.errors import ResearchError
from researchx.state.models import PlanProposal, ResearchObjective
from researchx.state.runtime import ResearchAgentRuntime
from researchx.state.store import ResearchStore
from researchx.tools import RESEARCH_EXCLUDED_TOOLS, create_research_tool_registry
from researchx.tools.base import ToolExecutionContext
from researchx.tools.dispatch_subagents_tool import (
    DispatchInput,
    SubagentTask,
    dispatch_subagents,
)
from tests.test_research.test_report_runtime import checked_evidence, task


@pytest.fixture
async def project(tmp_path):
    store = ResearchStore(tmp_path, "e" * 12, root=tmp_path / "memory")
    (await store.capture(origin_id="user", kind="user", content="独立研究"))
    runtime = ResearchAgentRuntime(store, workspace_root=tmp_path / "workspaces")
    await runtime.start(
        ResearchObjective(
            project_id="P",
            subject="公司 P",
            report_type="earnings",
            requirements=["parent"],
            deliverables=["note"],
        )
    )
    (await runtime.repository.commit_plan(
        "P",
        PlanProposal(objective_revision=1, tasks=[task("parent")], rationale="研究"),
        (await store.load()).revision,
    ))
    (await runtime.repository.transition_task(
        "parent", "ready", "in_progress", (await store.load()).revision, task_revision=1
    ))
    return runtime


def latest_result(request):
    return next(
        block
        for message in reversed(request.messages)
        for block in reversed(message.content)
        if isinstance(block, ToolResultBlock)
    )


class ChildModel:
    def __init__(
        self, *, fail=(), wait=(), initial=None, evidence_refs=None, outputs=None, delay=0.02
    ):
        self.fail, self.wait, self.initial = set(fail), set(wait), initial or {}
        self.evidence_refs, self.outputs = evidence_refs or [], outputs
        self.delay = delay
        self.calls, self.requests = {}, []
        self.active, self.peak = 0, 0
        self.concurrency_target = 0
        self.concurrency_gate = asyncio.Event()
        self.waiting, self.completed, self.cancelled = asyncio.Event(), asyncio.Event(), []

    async def stream_message(self, request):
        self.requests.append(request)
        packet = json.loads(request.messages[0].text)
        key = packet["task_id"]
        call = self.calls.get(key, 0) + 1
        self.calls[key] = call
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.concurrency_target and call == 1:
                if self.active >= self.concurrency_target:
                    self.concurrency_gate.set()
                await asyncio.wait_for(self.concurrency_gate.wait(), 10)
            await asyncio.sleep(self.delay)
            if key in self.fail:
                raise RuntimeError(f"fixture failure: {key}")
            if key in self.wait and call >= 2:
                self.waiting.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled.append(key)
                    raise
            if call == 1:
                name, arguments = self.initial.get(
                    key, ("write_file", {"path": "note.md", "content": f"candidate_{key}"})
                )
            else:
                name, arguments = (
                    "submit_subagent_result",
                    {
                        "summary": f"summary_{key}; source pending verification",
                        "evidence_refs": self.evidence_refs,
                        "output_paths": self.outputs
                        if self.outputs is not None
                        else (["note.md"] if key not in self.initial else []),
                    },
                )
                if key not in self.wait:
                    self.completed.set()
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(
                    role="assistant",
                    reasoning_content="private-replay",
                    content=[ToolUseBlock(id=f"{key}-{call}", name=name, input=arguments)],
                ),
                usage=UsageSnapshot(input_tokens=5, output_tokens=3),
            )
        finally:
            self.active -= 1


async def context(runtime, model, *, checker=None, **metadata):
    registry = create_research_tool_registry()
    checker = checker or PermissionChecker(
        PermissionSettings(allowed_tools=["write_file", "edit_file"])
    )
    usage = []
    values = {
        "research_store": runtime.store,
        "research_runtime": runtime,
        "account_subagent_usage": usage.append,
        **metadata,
    }
    query = QueryContext(
        api_client=model,
        tool_registry=registry,
        permission_checker=checker,
        cwd=runtime.store.directory.parent,
        model="fixture",
        system_prompt="main",
        max_tokens=4096,
        context_window_tokens=200000,
        tool_metadata=values,
    )
    return ToolExecutionContext(
        cwd=(await runtime.resolve_workspace("P")), metadata={**values, "query_context": query}
    ), usage


def assignments(*keys):
    return [
        SubagentTask(task_id=key, instruction=f"independent_{key}", context=f"context_{key}")
        for key in keys
    ]


async def test_repeated_authority_violation_stops_child_before_budget_or_timeout(project):
    class ViolatingModel(ChildModel):
        async def stream_message(self, request):
            self.requests.append(request)
            call = len(self.requests)
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(
                    role="assistant",
                    content=[
                        ToolUseBlock(
                            id=f"different-id-{call}",
                            name="research_memory",
                            input={
                                "operation": {
                                    "action": "update_task",
                                    "task_id": f"task-{call}",
                                    "status": "completed",
                                }
                            },
                        )
                    ],
                ),
                usage=UsageSnapshot(input_tokens=5, output_tokens=3),
            )

    model = ViolatingModel()
    ctx, usage = await context(project, model, subagent_max_calls=20)
    before = (await project.store.load()).model_dump()
    result = await dispatch_subagents(assignments("one"), ctx)
    assert len(model.requests) == 2 and len(usage) == 2
    assert result.results[0].status == "failed"
    assert "repeatedly attempted" in " ".join(result.results[0].errors)
    assert (await project.store.load()).model_dump() == before
    first_feedback = latest_result(model.requests[1])
    assert first_feedback.result_metadata["error_code"] == "subagent_authority_denied"
    assert "submit_subagent_result" in first_feedback.content
    rows = await ConversationRecords(current_database(), project.store.cwd).list(channel="subagent")
    saved = await ConversationRecords(current_database(), project.store.cwd).load(
        rows[0]["session_id"]
    )
    results = [
        block
        for message in saved["messages"]
        for block in message["content"]
        if block["type"] == "tool_result"
    ]
    assert len(results) == 2 and {block["tool_use_id"] for block in results} == {
        "different-id-1",
        "different-id-2",
    }


async def test_one_authority_denial_can_be_repaired_by_candidate_submission(project):
    model = ChildModel(
        initial={
            "one": (
                "research_memory",
                {
                    "operation": {
                        "action": "update_task",
                        "task_id": "research",
                        "status": "completed",
                    }
                },
            )
        }
    )
    ctx, _ = await context(project, model)
    result = await dispatch_subagents(assignments("one"), ctx)
    assert result.results[0].status == "completed" and len(model.requests) == 2


async def test_revoked_candidate_stops_child_without_resubmission_loop(project):
    class ChangingScope(ChildModel):
        async def stream_message(self, request):
            if self.requests:
                await project.submit_feedback("P", "修改目标")
            async for event in super().stream_message(request):
                yield event

    model = ChangingScope()
    ctx, _ = await context(project, model, subagent_max_calls=20)
    result = await dispatch_subagents(assignments("one"), ctx)
    assert len(model.requests) == 2
    assert result.results[0].status == "failed"
    assert "child execution revoked" in " ".join(result.results[0].errors)
    assert (await project.store.load()).project.objective_revision == 2


async def test_single_child_returns_real_paths_and_does_not_mutate_parent(project):
    model = ChildModel()
    ctx, usage = (await context(project, model))
    before = (await project.store.load()).model_dump()
    memory = (await project.load_workspace_memory("P"))
    result = await dispatch_subagents(assignments("one"), ctx)
    child = result.results[0]
    assert child.status == "completed" and child.summary.startswith("summary_one")
    assert len(child.output_paths) == 1
    path = (await project.resolve_workspace("P")) / child.output_paths[0]
    assert path.read_text() == "candidate_one" and not Path(child.output_paths[0]).is_absolute()
    assert (
        (await project.store.load()).model_dump() == before and (await project.load_workspace_memory("P")) == memory
    )
    assert len(usage) == 2
    transcript = json.dumps((await ConversationRecords(current_database(), project.store.cwd).load(
        subagent_session_id(project.store.session_id, Path("dispatches") / result.dispatch_id / "one/messages.json")
    ))["messages"])
    assert "private-replay" not in transcript and "reasoning_content" not in transcript
    child_tools = {tool["name"] for tool in model.requests[0].tools}
    assert {
        "planner",
        "replanner",
        "research_project",
        "dispatch_subagents",
        "investigate_conflict",
        "bash",
    }.isdisjoint(child_tools)
    schema = next(
        tool["input_schema"]
        for tool in model.requests[0].tools
        if tool["name"] == "research_memory"
    )
    assert schema["$defs"]["ReadMemory"]["properties"]["action"]["const"] == "read"


@pytest.mark.parametrize("limit", [1, 2, 3])
async def test_three_children_obey_config_and_have_independent_histories(project, limit):
    model = ChildModel(delay=0.04)
    model.concurrency_target = limit  # Verify overlap with a barrier, not database timing.
    ctx, _ = (await context(project, model, subagent_max_concurrency=limit))
    result = await dispatch_subagents(assignments("industry", "financial", "storage"), ctx)
    assert [child.task_id for child in result.results] == ["industry", "financial", "storage"]
    assert all(child.status == "completed" for child in result.results)
    assert model.peak == limit
    assert len({child.output_paths[0] for child in result.results}) == 3
    for request in model.requests:
        key = json.loads(request.messages[0].text)["task_id"]
        text = " ".join(message.text for message in request.messages)
        for other in {"industry", "financial", "storage"} - {key}:
            assert f"independent_{other}" not in text and f"candidate_{other}" not in text


async def test_concurrency_limit_is_shared_across_dispatch_calls(project):
    model = ChildModel(delay=0.04)
    model.concurrency_target = 2  # Verify overlap with a barrier, not database timing.
    ctx, _ = (await context(project, model, subagent_max_concurrency=2))
    first, second = await asyncio.gather(
        dispatch_subagents(assignments("a", "b"), ctx),
        dispatch_subagents(assignments("c", "d"), ctx),
    )
    assert model.peak == 2 and first.dispatch_id != second.dispatch_id
    assert all(item.status == "completed" for item in first.results + second.results)


async def test_failure_isolated_and_results_keep_input_order(project):
    model = ChildModel(fail=["bad"])
    ctx, _ = (await context(project, model))
    result = await dispatch_subagents(assignments("good", "bad", "also_good"), ctx)
    assert [child.status for child in result.results] == ["completed", "failed", "completed"]
    assert "fixture failure" in result.results[1].errors[0]
    assert result.results[0].output_paths and result.results[2].output_paths


async def test_parent_cancellation_settles_active_and_queued_children(project):
    model = ChildModel(wait=["active"])
    ctx, _ = (await context(project, model, subagent_max_concurrency=1))
    pending = asyncio.create_task(dispatch_subagents(assignments("active", "queued"), ctx))
    await asyncio.wait_for(model.waiting.wait(), timeout=3)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert model.cancelled == ["active"] and model.active == 0 and "queued" not in model.calls
    audit = (await dispatch_summaries(project.store, "P"))[0]
    assert audit["status"] == "cancelled" and [item["status"] for item in audit["results"]] == [
        "cancelled",
        "cancelled",
    ]
    path = (await project.resolve_workspace("P")) / audit["results"][0]["output_paths"][0]
    assert path.read_text() == "candidate_active"


async def test_completed_sibling_is_preserved_when_parent_is_cancelled(project):
    model = ChildModel(wait=["slow"])
    ctx, _ = (await context(project, model))
    pending = asyncio.create_task(dispatch_subagents(assignments("fast", "slow"), ctx))
    await asyncio.wait_for(model.waiting.wait(), timeout=3)
    async def completed_sibling():
        while True:
            batches = await dispatch_summaries(project.store, "P")
            if batches and any(item["task_id"] == "fast" and item["status"] == "completed"
                               for item in (batches[0]["results"] or [])):
                return
            await asyncio.sleep(0.01)
    await asyncio.wait_for(completed_sibling(), timeout=3)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    audit = (await dispatch_summaries(project.store, "P"))[0]
    assert [item["status"] for item in audit["results"]] == ["completed", "cancelled"]
    assert ((await project.resolve_workspace("P")) / audit["results"][0]["output_paths"][0]).is_file()


@pytest.mark.parametrize(
    "settings,error",
    [
        # Allow the real PostgreSQL write/receipt to finish before testing
        # timeout preservation; the strict early deadline is tested separately.
        ({"subagent_timeout_seconds": 1.0}, "TimeoutError"),
        ({"subagent_max_calls": 1}, "MaxTurnsExceeded"),
    ],
)
async def test_limits_fail_only_the_child_and_preserve_files(project, settings, error):
    model = ChildModel(wait=["one"])
    ctx, _ = (await context(project, model, **settings))
    result = await dispatch_subagents(assignments("one"), ctx)
    assert result.results[0].status == "failed" and error in result.results[0].errors[0]
    assert result.results[0].output_paths and model.active == 0


async def test_early_deadline_cancels_child_before_side_effects(project):
    model = ChildModel(delay=1)
    ctx, _ = await context(project, model, subagent_timeout_seconds=0.001)
    result = await asyncio.wait_for(dispatch_subagents(assignments("one"), ctx), timeout=3)
    assert result.results[0].status == "failed"
    assert "TimeoutError" in result.results[0].errors[0]
    assert result.results[0].output_paths == []
    assert model.active == 0


@pytest.mark.parametrize("key", ["../escape", "/tmp/absolute", "x/y", "x.y", "", "a" * 65])
def test_task_paths_are_not_model_controlled(key):
    with pytest.raises(ValidationError):
        DispatchInput(tasks=[{"task_id": key, "instruction": "research"}])


def test_batch_size_and_duplicate_ids_are_rejected():
    for values in [[], assignments("a", "a"), assignments(*(f"x{i}" for i in range(17)))]:
        with pytest.raises(ValidationError):
            DispatchInput(tasks=values)


@pytest.mark.parametrize(
    "tool", ["planner", "replanner", "research_project", "dispatch_subagents", "bash"]
)
async def test_child_cannot_invoke_authoritative_or_recursive_tools(project, tool):
    model = ChildModel(initial={"one": (tool, {})})
    ctx, _ = (await context(project, model))
    before = (await project.store.load()).model_dump()
    result = await dispatch_subagents(assignments("one"), ctx)
    assert result.results[0].status == "completed"
    call = latest_result(model.requests[1])
    assert call.is_error and "Unknown tool" in call.content
    assert (await project.store.load()).model_dump() == before


@pytest.mark.parametrize(
    "action", ["set_context", "create_plan", "update_task", "add_evidence", "add_conclusion"]
)
async def test_child_memory_schema_exposes_only_read(project, action):
    model = ChildModel(initial={"one": ("research_memory", {"operation": {"action": action}})})
    ctx, _ = (await context(project, model))
    before = (await project.store.load()).revision
    result = await dispatch_subagents(assignments("one"), ctx)
    assert result.results[0].status == "completed" and latest_result(model.requests[1]).is_error
    assert (await project.store.load()).revision == before


@pytest.mark.parametrize(
    "kind", ["main_memory", "parent_artifact", "outside", "traversal", "symlink"]
)
async def test_child_file_writes_are_confined_to_own_directory(project, tmp_path, kind):
    workspace = (await project.resolve_workspace("P"))
    before = (await project.load_workspace_memory("P"))
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (workspace / "artifacts/link.txt").symlink_to(outside)
    candidate = {
        "main_memory": str(workspace / "MEMORY.md"),
        "parent_artifact": str(workspace / "artifacts/main.txt"),
        "outside": str(outside),
        "traversal": "../escaped.txt",
        "symlink": str(workspace / "artifacts/link.txt"),
    }[kind]
    model = ChildModel(initial={"one": ("write_file", {"path": candidate, "content": "overwrite"})})
    ctx, _ = (await context(project, model))
    result = await dispatch_subagents(assignments("one"), ctx)
    assert result.results[0].status == "completed" and latest_result(model.requests[1]).is_error
    assert (await project.load_workspace_memory("P")) == before and outside.read_text() == "outside"
    assert not (workspace / "artifacts/main.txt").exists()


async def test_child_can_read_main_memory_and_read_only_evidence(project):
    _, evidence = (await checked_evidence(project.repository, "parent"))
    for name, values in [
        ("read_file", {"path": str((await project.resolve_workspace("P")) / "MEMORY.md")}),
        (
            "research_memory",
            {"operation": {"action": "read", "ids": [evidence], "include_content": True}},
        ),
    ]:
        model = ChildModel(initial={"one": (name, values)}, evidence_refs=[evidence])
        ctx, _ = (await context(project, model))
        result = await dispatch_subagents(assignments("one"), ctx)
        assert result.results[0].status == "completed" and result.results[0].evidence_refs == [
            evidence
        ]
        assert not latest_result(model.requests[1]).is_error


@pytest.mark.parametrize(
    "outputs,refs,error",
    [
        (["missing.md"], [], "does not exist"),
        (["../other.md"], [], "traversal"),
        (["/tmp/other.md"], [], "must be relative"),
        ([], ["ev_fabricated"], "Unverifiable"),
    ],
)
async def test_fabricated_paths_and_evidence_are_not_returned_as_success(
    project, outputs, refs, error
):
    model = ChildModel(evidence_refs=refs, outputs=outputs)
    ctx, _ = (await context(project, model, subagent_max_calls=2))
    result = await dispatch_subagents(assignments("one"), ctx)
    assert result.results[0].status == "failed" and not result.results[0].evidence_refs
    audit = json.dumps((await ConversationRecords(current_database(), project.store.cwd).load(
        subagent_session_id(project.store.session_id, Path("dispatches") / result.dispatch_id / "one/messages.json")
    ))["messages"])
    # Failed submission is present in the public transcript even when the turn cap stops repair.
    assert error in audit


async def test_repeated_dispatches_never_overwrite_previous_outputs(project):
    ctx, _ = (await context(project, ChildModel()))
    first = await dispatch_subagents(assignments("one"), ctx)
    ctx.metadata["query_context"].api_client = ChildModel()
    second = await dispatch_subagents(assignments("one"), ctx)
    assert first.dispatch_id != second.dispatch_id
    assert first.results[0].output_paths != second.results[0].output_paths
    assert (
        (await project.resolve_workspace("P")) / first.results[0].output_paths[0]
    ).read_text() == "candidate_one"


async def test_recursion_is_rejected_even_with_direct_function_call(project):
    ctx, _ = (await context(project, ChildModel(), subagent_child=True))
    with pytest.raises(ResearchError, match="Recursive"):
        await dispatch_subagents(assignments("one"), ctx)


async def test_parent_permission_remains_effective(project):
    model = ChildModel()
    checker = PermissionChecker(PermissionSettings(denied_tools=["write_file"]))
    ctx, _ = (await context(project, model, checker=checker))
    result = await dispatch_subagents(assignments("one"), ctx)
    assert (
        result.results[0].status == "failed" and "禁止" in latest_result(model.requests[1]).content
    )
    assert not list((await project.resolve_workspace("P")).glob("subagents/*/one/note.md"))


async def test_real_tool_loop_binds_dispatch_to_parent_execution(project):
    model = ChildModel()
    ctx, _ = (await context(project, model))
    query = ctx.metadata["query_context"]
    result = await _execute_tool_call(
        query,
        "dispatch_subagents",
        "parent-dispatch",
        {"tasks": [task.model_dump(mode="json") for task in assignments("one")]},
    )
    assert not result.is_error
    execution = (await project.store.load()).executions[result.result_metadata["execution_id"]]
    assert execution.status == "committed" and execution.task_id == "parent"
    assert project.repository._plan((await project.store.load())).tasks[0].status == "in_progress"


async def test_removed_tools_are_uncallable_and_undiscoverable_in_research_mode(project):
    ctx, _ = (await context(project, ChildModel()))
    registry = ctx.metadata["query_context"].tool_registry
    search = registry.get("tool_search")
    for name in RESEARCH_EXCLUDED_TOOLS:
        assert registry.get(name) is None and name not in {
            tool["name"] for tool in registry.to_api_schema()
        }
        result = await search.execute(
            search.input_model(query=name),
            ToolExecutionContext(cwd=ctx.cwd, metadata={"tool_registry": registry}),
        )
        assert name not in {line.split(":", 1)[0] for line in result.output.splitlines()}
        result = await _execute_tool_call(
            ctx.metadata["query_context"], name, f"removed-{name}", {}
        )
        assert result.is_error and "Unknown tool" in result.content
    assert {"planner", "replanner", "dispatch_subagents"} <= {
        tool.name for tool in registry.list_tools()
    }
    general = create_research_tool_registry(mode="general")
    assert RESEARCH_EXCLUDED_TOOLS.isdisjoint(tool.name for tool in general.list_tools())
    assert {tool.name for tool in general.list_tools()} == {
        tool.name for tool in registry.list_tools()
    }


def test_subagent_config_is_small_and_bounded():
    config = ResearchMemorySettings()
    assert config.subagent_max_concurrency == 3 and config.subagent_max_calls == 12
    with pytest.raises(ValidationError):
        ResearchMemorySettings(subagent_max_concurrency=0)


@pytest.mark.parametrize("kind", ["sibling", "other_project", "symlink", "traversal"])
async def test_child_cannot_read_unauthorized_files(project, tmp_path, kind):
    workspace = (await project.resolve_workspace("P"))
    sibling = workspace / "subagents/old-dispatch/sibling/private.md"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("SIBLING_PRIVATE", encoding="utf-8")
    other = tmp_path / "workspaces/other_project/MEMORY.md"
    other.parent.mkdir(parents=True)
    other.write_text("OTHER_PROJECT_PRIVATE", encoding="utf-8")
    link = workspace / "artifacts/link.md"
    link.symlink_to(other)
    path = {
        "sibling": str(sibling),
        "other_project": str(other),
        "symlink": str(link),
        "traversal": "../private.md",
    }[kind]
    model = ChildModel(initial={"one": ("read_file", {"path": path})})
    ctx, _ = (await context(project, model))
    result = await dispatch_subagents(assignments("one"), ctx)
    receipt = latest_result(model.requests[1])
    assert result.results[0].status == "completed" and receipt.is_error
    assert (
        "SIBLING_PRIVATE" not in receipt.content and "OTHER_PROJECT_PRIVATE" not in receipt.content
    )


@pytest.mark.parametrize("name", ["glob", "grep"])
async def test_child_search_filters_sibling_and_symlink_content(project, tmp_path, name):
    workspace = (await project.resolve_workspace("P"))
    visible = workspace / "artifacts/shared.md"
    visible.write_text("SEARCH_VISIBLE", encoding="utf-8")
    from researchx.state.models import SourceUsage
    await project.store.capture(origin_id="public-shared-fixture", kind="file",
        locator=str(visible), content=visible.read_text(),
        usage=SourceUsage(category="public", licence_status="pending",
            model_use_allowed=True, internal_export_allowed=True, completeness="full"))
    sibling = workspace / "subagents/old-dispatch/sibling/private.md"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("SEARCH_SIBLING_PRIVATE", encoding="utf-8")
    private = tmp_path / "private.md"
    private.write_text("SEARCH_LINK_PRIVATE", encoding="utf-8")
    (workspace / "artifacts/link.md").symlink_to(private)
    arguments = {"root": str(workspace), "pattern": "**/*.md" if name == "glob" else "SEARCH_"}
    model = ChildModel(initial={"one": (name, arguments)})
    ctx, _ = (await context(project, model))
    result = await dispatch_subagents(assignments("one"), ctx)
    receipt = latest_result(model.requests[1])
    assert result.results[0].status == "completed" and not receipt.is_error
    assert (
        "shared.md" in receipt.content
        and "private.md" not in receipt.content
        and "link.md" not in receipt.content
    )
    assert (
        "SEARCH_SIBLING_PRIVATE" not in receipt.content
        and "SEARCH_LINK_PRIVATE" not in receipt.content
    )


@pytest.mark.parametrize("change", ["corrupt", "supersede"])
async def test_candidate_evidence_checks_hash_and_current_version(project, change):
    from tests.test_research.test_conflicts import apply

    _, evidence_id = (await checked_evidence(project.repository, "parent"))
    evidence = (await project.store.load()).evidence_pool[evidence_id]
    if change == "corrupt":
        source = (await project.store.load()).sources[evidence.source_id]
        (await source_path(project.store, source)).write_text("tampered", encoding="utf-8")
    else:
        (await apply(
            project.store,
            "add_evidence",
            source_id=evidence.source_id,
            statement="新版待核实",
            supersedes=evidence_id,
        ))
    model = ChildModel(evidence_refs=[evidence_id])
    ctx, _ = (await context(project, model, subagent_max_calls=2))
    result = await dispatch_subagents(assignments("one"), ctx)
    assert result.results[0].status == "failed" and not result.results[0].evidence_refs


async def test_plain_answer_is_not_accepted_as_structured_success(project):
    class PlainModel:
        async def stream_message(self, request):
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(role="assistant", content=[TextBlock(text="done")]),
                usage=UsageSnapshot(input_tokens=2, output_tokens=1),
            )

    ctx, _ = (await context(project, PlainModel()))
    result = await dispatch_subagents(assignments("one"), ctx)
    assert (
        result.results[0].status == "failed"
        and "without a validated" in result.results[0].errors[0]
    )


async def test_child_prompt_hooks_share_budget_and_keep_parent_context(project):
    from researchx.hooks import HookEvent, HookExecutionContext, HookExecutor
    from researchx.hooks.loader import HookRegistry
    from researchx.hooks.schemas import PromptHookDefinition

    class HookModel(ChildModel):
        async def stream_message(self, request):
            if "判断 ResearchX 中某个 Hook 条件" in request.system_prompt:
                yield ApiMessageCompleteEvent(
                    message=ConversationMessage.from_user_text('{"ok": true}'),
                    usage=UsageSnapshot(input_tokens=2, output_tokens=1),
                )
            else:
                async for event in super().stream_message(request):
                    yield event

    model = HookModel()
    ctx, usage = (await context(project, model, subagent_max_calls=2))
    registry = HookRegistry()
    registry.register(
        HookEvent.PRE_TOOL_USE, PromptHookDefinition(prompt="check", matcher="write_file")
    )
    hooks = HookExecutor(
        registry, HookExecutionContext(cwd=ctx.cwd, api_client=model, default_model="parent")
    )
    ctx.metadata["query_context"].hook_executor = hooks
    result = await dispatch_subagents(assignments("one"), ctx)
    assert (
        result.results[0].status == "failed" and "MaxTurnsExceeded" in result.results[0].errors[0]
    )
    assert len(usage) == 2 and sum(item.input_tokens for item in usage) == 7
    assert hooks._context.api_client is model and hooks._context.default_model == "parent"


async def test_scope_change_during_edit_approval_revokes_child_write(project):
    async def approve(path, diff, added, removed):
        await project.submit_feedback("P", "请调整研究目标并重新规划")
        return "accept"

    model = ChildModel()
    ctx, _ = (await context(project, model, edit_approval_prompt=approve, subagent_max_calls=2))
    result = await dispatch_subagents(assignments("one"), ctx)
    assert result.results[0].status == "failed" and not result.results[0].output_paths
    assert (
        latest_result(model.requests[1]).is_error
        and "revoked parent scope" in latest_result(model.requests[1]).content
    )
    assert (await project.store.load()).project.objective_revision == 2
    assert not list((await project.resolve_workspace("P")).glob("subagents/*/one/note.md"))


async def test_generic_child_cannot_call_planning_helper_directly(project):
    from researchx.tools.planner_tool import planner_tool

    ctx, _ = (await context(project, ChildModel(), subagent_child=True))
    with pytest.raises(ResearchError, match="child recursion"):
        await planner_tool("replace", ctx)


async def test_cancelled_parent_execution_restores_workspace_and_partial_candidates(
    project, tmp_path
):
    model = ChildModel(wait=["one"])
    ctx, _ = (await context(project, model))
    pending = asyncio.create_task(
        _execute_tool_call(
            ctx.metadata["query_context"],
            "dispatch_subagents",
            "interrupted",
            {"tasks": [item.model_dump(mode="json") for item in assignments("one")]},
        )
    )
    await asyncio.wait_for(model.waiting.wait(), timeout=3)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await project.interrupt("P", "Execution interrupted")
    saved_workspace = (await project.resolve_workspace("P"))
    memory_before = (await project.load_workspace_memory("P"))
    restored_store = ResearchStore(
        tmp_path, project.store.session_id, root=project.store.directory.parent
    )
    restored = ResearchAgentRuntime(restored_store, workspace_root=tmp_path / "changed-default")
    await restored.resume("P")
    assert (await restored.resolve_workspace("P")) == saved_workspace
    assert (await restored.load_workspace_memory("P")) == memory_before
    assert restored.repository._plan((await restored.store.load())).tasks[0].status == "ready"
    execution = next(
        item
        for item in (await restored.store.load()).executions.values()
        if item.tool_use_id == "interrupted"
    )
    assert execution.status == "cancelled"
    assert not any(
        source.title == "dispatch_subagents" for source in (await restored.store.load()).sources.values()
    )
    batch = (await dispatch_summaries(restored.store, "P"))[0]
    assert (
        batch["status"] == "cancelled"
        and (saved_workspace / batch["results"][0]["output_paths"][0]).is_file()
    )
    (await restored.repository.transition_task(
        "parent", "ready", "in_progress", (await restored.store.load()).revision, task_revision=1
    ))
    ctx, _ = (await context(restored, ChildModel()))
    retry = await dispatch_subagents(assignments("one"), ctx)
    assert retry.results[0].status == "completed" and retry.dispatch_id != batch["dispatch_id"]
