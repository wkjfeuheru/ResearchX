"""Adversarial integration through the original model/tool dispatch and host APIs."""

import asyncio
import json
import shlex
from pathlib import Path

from researchx.engine.messages import ToolResultBlock
from researchx.engine.query import QueryContext, _execute_tool_call
from researchx.engine.stream_events import ToolExecutionCompleted
from researchx.state.completion import CompletionPolicy
from researchx.tools import RESEARCH_EXCLUDED_TOOLS
from tests.test_research.agent_core_support import (
    FEEDBACK,
    REQUEST,
    AlphaModel,
    ScriptModel,
    await_point,
    collect,
    workspace_packet,
)
from tests.test_research.test_agent_core_e2e import (
    lab as lab,
    current_plan,
    main_requests,
    pause_after_research,
)


async def call_sequence(bundle, steps):
    """Stop after real tool completion events; no final prose bypasses completion guards."""
    driver = ScriptModel(steps)
    bundle.engine.set_api_client(driver)
    stream = bundle.engine.submit_message("检查当前研究项目的接口与状态")
    results = []
    try:
        async for event in stream:
            if isinstance(event, ToolExecutionCompleted):
                results.append(event)
                if len(results) == len(steps):
                    break
    finally:
        await stream.aclose()
    assert len(results) == len(steps)
    return results, driver


async def operation(store, action, **fields):
    return {
        "operation": {
            "action": action,
            "operation_id": f"e2e-{(await store.load()).revision}-{action}",
            "expected_revision": (await store.load()).revision,
            **fields,
        }
    }


async def stop(pending):
    pending.cancel()
    try:
        await pending
    except asyncio.CancelledError:
        pass


async def test_e2e_06_project_switch_files_context_and_child_boundaries(lab):
    class BetaModel(AlphaModel):
        def objective(self):
            return (
                super()
                .objective()
                .model_copy(update={"project_id": "Beta", "subject": "BETA_PRIVATE_BACKGROUND"})
            )

    other, other_store, _, _ = await lab.create(model_factory=BetaModel, session="b" * 12)
    await collect(other.engine)
    other_root = Path((await other_store.load()).project.workspace_path)
    bundle, store, model, _ = await lab.create()
    model.external_private_path = str(other_root / "MEMORY.md")
    await collect(bundle.engine)
    root = Path((await store.load()).project.workspace_path)
    assert root != other_root
    for request in main_requests(model)[2:]:
        assert "BETA_PRIVATE_BACKGROUND" not in " ".join(
            message.runtime_context or "" for message in request.messages
        )
        assert workspace_packet(request)["workspace_id"] == store.workspace_id
    children = [
        request
        for request in model.requests
        if "submit_subagent_result" in {tool["name"] for tool in request.tools}
    ]
    assert any(
        block.is_error and "outside" in block.content
        for request in children
        for message in request.messages
        for block in message.content
        if isinstance(block, ToolResultBlock)
    )
    results, _ = await call_sequence(
        bundle,
        [
            ("read_file", {"path": str(other_root / "MEMORY.md")}),
            ("read_file", {"path": "artifacts/research-1.md"}),
            ("read_file", {"path": "../MEMORY.md"}),
        ],
    )
    assert results[0].is_error and "outside" in results[0].output
    assert not results[1].is_error and "AlphaTech" in results[1].output
    assert results[2].is_error and "traversal" in results[2].output
    assert "BETA_PRIVATE_BACKGROUND" in (other_root / "MEMORY.md").read_text()


async def test_e2e_09_stale_patch_rejected_then_main_replans(lab):
    class StaleOnce(AlphaModel):
        stale = True

        def patch(self, packet):
            patch = super().patch(packet)
            if self.stale and packet["plan"]["revision"] == 2:
                self.stale = False
                patch["base_plan_revision"] = 1
            return patch

    bundle, store, model, _, pending, events = await pause_after_research(
        lab, model_factory=StaleOnce
    )
    model.allowed_errors.add("Plan revision conflict")
    runtime = bundle.engine.tool_metadata["research_runtime"]
    model.pause_on_task = "overseas"
    await runtime.submit_feedback("Alpha", FEEDBACK)
    model.release_main.set()
    await await_point(model.task_paused, pending)
    old_plan_id = (await current_plan(store)).id
    assert (await current_plan(store)).revision == 2
    await runtime.submit_feedback("Alpha", FEEDBACK)
    model.release_task.set()
    await asyncio.wait_for(pending, timeout=60)
    rejected = [
        event for event in events if isinstance(event, ToolExecutionCompleted) and event.is_error
    ]
    assert (
        len(rejected) == 1
        and rejected[0].tool_name == "replanner"
        and "Plan revision conflict" in rejected[0].output
    )
    state_at_rejection = model.observed_errors[0][1]
    assert state_at_rejection["research_state"]["current_plan_id"] == old_plan_id
    assert state_at_rejection["project"]["plan_revision"] == 2
    assert (
        len(model.planning_packets) == 4
    )  # Initial planner, v2 patch, rejected v1 patch, repaired v3 patch.
    assert (await current_plan(store)).revision == 3 and (
        await store.load()
    ).project.status == "completed"


async def test_e2e_10_late_children_cannot_pollute_revised_plan(lab):
    bundle, store, model, _ = await lab.create(hold=["company", "industry", "financial"])
    model.allowed_errors.add("Late tool result discarded")
    events = []

    async def run():
        async for event in bundle.engine.submit_message(REQUEST):
            events.append(event)

    pending = lab.start(run())
    await await_point(model.child_waiting, pending)
    runtime = bundle.engine.tool_metadata["research_runtime"]
    old_execution = next(
        item for item in (await store.load()).executions.values() if item.status == "running"
    )
    await runtime.submit_feedback("Alpha", FEEDBACK)
    # A trusted host races a validated planning invocation against a delayed
    # provider. Use the same main context and dispatcher, with no second loop.
    context = QueryContext(
        api_client=model,
        tool_registry=bundle.tool_registry,
        permission_checker=runtime.permission_checker,
        cwd=Path(bundle.cwd),
        model=bundle.engine.model,
        system_prompt="main",
        max_tokens=4096,
        context_window_tokens=1000000,
        tool_metadata=bundle.engine.tool_metadata,
    )
    patch = await _execute_tool_call(
        context, "replanner", "host-racing-replan", {"reason": FEEDBACK}
    )
    assert not patch.is_error and (await current_plan(store)).revision == 2
    model.hold.clear()
    model.release_child.set()
    await asyncio.wait_for(pending, timeout=60)
    memory = await store.load()
    assert memory.executions[old_execution.id].status == "rejected"
    assert not any(
        source.origin_id == old_execution.tool_use_id for source in memory.sources.values()
    )
    from researchx.state.dispatch_audit import dispatch_summaries

    audits = await dispatch_summaries(store, "Alpha")
    old = next(item for item in audits if item["baseline"][2] == 1)
    assert all(item["status"] == "failed" and not item["evidence_refs"] for item in old["results"])
    assert all(item["output_paths"] for item in old["results"])
    assert any(
        event.is_error and "Late tool result" in event.output
        for event in events
        if isinstance(event, ToolExecutionCompleted)
    )
    assert (await current_plan(store)).revision == 2 and memory.project.status == "completed"
    assert all(not item.stale and item.plan_revision == 2 for item in memory.artifacts.values())


async def test_e2e_11_rejects_premature_task_then_main_repairs(lab, monkeypatch):
    statuses = []
    inspect = CompletionPolicy.inspect_task

    def observe(policy, task, context):
        statuses.append((task.id, task.status))
        return inspect(policy, task, context)

    monkeypatch.setattr(CompletionPolicy, "inspect_task", observe)
    bundle, store, model, _ = await lab.create(premature=True)
    events = await collect(bundle.engine)
    checks = [
        json.JSONDecoder().raw_decode(event.output)[0]
        for event in events
        if isinstance(event, ToolExecutionCompleted)
        and event.tool_name == "research_project"
        and event.output.startswith("{")
    ]
    checks = [check for check in checks if "passed" in check]
    failed = next(check for check in checks if not check["passed"])
    assert failed["missing_requirements"] and failed["recommended_actions"]
    assert ("research", "validating") in statuses and ("draft", "validating") in statuses
    assert (
        len(
            [
                entry
                for entry in (await store.load()).history
                if entry["action"] == "task_completion_check"
            ]
        )
        == 3
    )
    assert all(task.status == "completed" for task in (await current_plan(store)).tasks)
    assert len(model.results) == 1


async def test_e2e_12_completion_idempotency_and_dependency_readiness(lab):
    async def _deferred_step_3(_):
        return ("research_project", await operation(store, "resume"))

    async def _deferred_step_4(_):
        return (
            "research_project",
            await operation(store, "complete_task", task_id="research", task_revision=1),
        )

    bundle, store, model, _, pending, _ = await pause_after_research(lab)
    await stop(pending)
    completed = (await current_plan(store)).tasks[0].model_dump()
    complete = next(
        fields
        for name, fields in model.calls
        if name == "research_project" and fields["operation"]["action"] == "complete_task"
    )
    results, _ = await call_sequence(
        bundle,
        [
            _deferred_step_3,
            ("research_project", complete),
            _deferred_step_4,
        ],
    )
    assert (
        not results[0].is_error
        and not results[1].is_error
        and json.JSONDecoder().raw_decode(results[1].output)[0]["passed"]
    )
    assert results[2].is_error and "Invalid task transition" in results[2].output
    assert (await current_plan(store)).tasks[0].model_dump() == completed
    assert (await current_plan(store)).tasks[1].status == "ready" and len(
        (await store.load()).artifacts
    ) == 1


async def test_e2e_14_markdown_completion_claim_cannot_change_authority(lab):
    async def _deferred_step_5(_):
        return ("research_project", await operation(store, "resume"))

    async def _deferred_step_6(_):
        return (
            "research_project",
            await operation(store, "claim_task", task_id="draft", task_revision=1),
        )

    async def _deferred_step_7(_):
        return ("research_project", await operation(store, "finalize"))

    bundle, store, model, _, pending, _ = await pause_after_research(lab)
    await stop(pending)
    before_records = {
        key: value.model_dump() for key, value in (await store.load()).evidence_pool.items()
    }
    results, driver = await call_sequence(
        bundle,
        [
            _deferred_step_5,
            _deferred_step_6,
            (
                "edit_file",
                {"path": "MEMORY.md", "old_str": "摘要尚待交付", "new_str": "所有研究任务已经完成"},
            ),
            _deferred_step_7,
            ("research_project", {"operation": {"action": "read"}}),
        ],
    )
    assert not results[2].is_error
    assert "所有研究任务已经完成" in (Path((await store.load()).project.workspace_path) / "MEMORY.md").read_text()
    assert "所有研究任务已经完成" not in str(workspace_packet(driver.requests[-1]))
    completion = json.JSONDecoder().raw_decode(results[3].output)[0]
    assert not completion["passed"] and any(
        "All required tasks must complete" in value for value in completion["missing_requirements"]
    )
    assert (await current_plan(store)).tasks[1].status == "in_progress" and (
        await store.load()
    ).project.status == "running"
    assert before_records == {
        key: value.model_dump() for key, value in (await store.load()).evidence_pool.items()
    }


async def test_tool_registry_discovery_and_execution_compatibility(lab):
    bundle, store, _, _ = await lab.create()
    # No project is implicitly created by ordinary registry inspection.
    results, driver = await call_sequence(
        bundle,
        [
            ("tool_search", {"query": "planner"}),
            ("tool_search", {"query": "dispatch_subagents"}),
            ("list_mcp_resources", {}),
            ("sleep", {"seconds": 0}),
            ("dispatch_subagents", {"tasks": [{"task_id": "one", "instruction": "research"}]}),
        ],
    )
    names = {item["name"] for item in driver.requests[0].tools}
    assert {
        "planner",
        "replanner",
        "dispatch_subagents",
        "research_memory",
        "research_project",
        "ask_user_question",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "bash",
        "web_search",
        "web_fetch",
        "image_to_text",
        "skill",
        "tool_search",
        "list_mcp_resources",
        "read_mcp_resource",
    } <= names
    assert RESEARCH_EXCLUDED_TOOLS.isdisjoint(names)
    assert "planner:" in results[0].output and "replanner:" in results[0].output
    assert "dispatch_subagents:" in results[1].output and not results[2].is_error
    assert results[3].is_error and "Unknown tool" in results[3].output
    assert results[4].is_error and results[4].metadata["error_code"] == "research_task_required"
    assert (await store.load()).project is None


async def test_report_shell_cwd_and_absolute_python_are_confined(lab):
    async def _deferred_step_8(_):
        return ("research_project", await operation(store, "resume"))

    async def _deferred_step_9(_):
        return (
            "research_project",
            await operation(store, "claim_task", task_id="draft", task_revision=1),
        )

    bundle, store, _, _, pending, _ = await pause_after_research(lab)
    await stop(pending)
    outside = lab.root / "outside-private.md"
    outside.write_text("OUTSIDE_HOST_FIXTURE", encoding="utf-8")
    code = f"from pathlib import Path; print(Path({str(outside)!r}).read_text())"
    results, _ = await call_sequence(
        bundle,
        [
            _deferred_step_8,
            _deferred_step_9,
            ("bash", {"command": "pwd", "cwd": str(lab.root)}),
            ("bash", {"command": "python3 -c " + shlex.quote(code)}),
        ],
    )
    assert results[2].is_error and "outside" in results[2].output
    assert results[3].is_error
    assert "OUTSIDE_HOST_FIXTURE" not in results[3].output


async def test_e2e_16_process_recovery_revokes_lease_between_tool_executions(lab):
    async def _deferred_step_10(_):
        return ("research_project", await operation(store, "resume"))

    async def _deferred_step_11(_):
        return (
            "research_project",
            await operation(store, "claim_task", task_id="draft", task_revision=1),
        )

    from researchx.state.repository import ResearchRepository
    from researchx.state.store import ResearchStore

    bundle, store, _, _, pending, _ = await pause_after_research(lab)
    await stop(pending)
    await call_sequence(
        bundle,
        [
            _deferred_step_10,
            _deferred_step_11,
        ],
    )
    # The process can die while waiting for a model, after claim_task persisted
    # but before begin_execution. Reload the actual checkpoint used by Web startup.
    assert (await store.load()).research_state.current_task_id == "draft"
    assert not any(item.status == "running" for item in (await store.load()).executions.values())
    reloaded = ResearchStore(lab.root, store.session_id, root=lab.root / "state")
    repository = ResearchRepository(reloaded)
    (await repository.recover())
    assert (await reloaded.load()).project.status == "suspended"
    assert (await current_plan(reloaded)).tasks[1].status == "blocked"
    assert (await reloaded.load()).research_state.current_task_id is None
    revision = (await reloaded.load()).revision
    (await repository.recover())
    assert (await reloaded.load()).revision == revision


async def test_generic_conflict_candidates_bind_existing_evidence(lab):
    async def _deferred_step_1(_):
        return ("research_project", await operation(store, "resume"))

    async def _deferred_step_2(_):
        return (
            "research_project",
            await operation(store, "claim_task", task_id="draft", task_revision=1),
        )

    class ConflictModel(AlphaModel):
        async def child(self, request):
            async for event in super().child(request):
                call = event.message.tool_uses[0]
                ids = self.checked((await self.store.load()))[:2]
                if call.name == "read_file":
                    call.name = "research_memory"
                    call.input = {
                        "operation": {"action": "read", "ids": ids, "include_content": True}
                    }
                if call.name == "submit_subagent_result":
                    call.input.update(
                        summary="冲突候选：比较两条披露的范围，需主 Agent 审查", evidence_refs=ids
                    )
                yield event

    bundle, store, _, _, pending, _ = await pause_after_research(lab)
    await stop(pending)
    child_model = ConflictModel(store, keys=["conflict"], concurrency=1)
    # The script controls the main agent, while the same transport serves children.
    driver = ScriptModel(
        [
            _deferred_step_1,
            _deferred_step_2,
            (
                "dispatch_subagents",
                {
                    "tasks": [
                        {
                            "task_id": "conflict",
                            "instruction": "比较现存披露，整理冲突候选",
                            "context": "不得裁决或修改主结论",
                        }
                    ]
                },
            ),
        ]
    )
    original_stream = driver.stream_message

    async def route(request):
        if "submit_subagent_result" in {tool["name"] for tool in request.tools}:
            async for event in child_model.stream_message(request):
                yield event
        else:
            async for event in original_stream(request):
                yield event

    driver.stream_message = route
    bundle.engine.set_api_client(driver)
    before = await store.load()
    result = None
    stream = bundle.engine.submit_message("用通用委托整理现有证据中的冲突候选")
    try:
        async for event in stream:
            if (
                isinstance(event, ToolExecutionCompleted)
                and event.tool_name == "dispatch_subagents"
            ):
                result = json.JSONDecoder().raw_decode(event.output)[0]
                assert not event.is_error
                break
    finally:
        await stream.aclose()
    assert result and result["results"][0]["status"] == "completed"
    assert result["results"][0]["evidence_refs"] == child_model.checked(before)[:2]
    assert "冲突候选" in result["results"][0]["summary"]
    after = await store.load()
    assert before.conclusions == after.conclusions and before.conflicts == after.conflicts
    assert (await current_plan(store)).tasks[1].status == "in_progress"
