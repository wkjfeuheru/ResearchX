"""E2E-01..16: real main loop, registry, runtime, state, filesystem and policy."""

import asyncio
import json
from pathlib import Path

import pytest

from researchx.engine.messages import ToolResultBlock
from researchx.engine.stream_events import AssistantTurnComplete, ToolExecutionCompleted
from researchx.state.completion import CompletionPolicy, ResearchContext
from researchx.workspace.session_files import SessionFiles
from tests.test_research.agent_core_support import (
    FEEDBACK,
    REQUEST,
    Lab,
    await_point,
    collect,
    workspace_packet,
)


@pytest.fixture
async def lab(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCHX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("RESEARCHX_DATA_DIR", str(tmp_path / "data"))

    async def forbid_network(*args, **kwargs):
        raise AssertionError("Agent core integration tests must not send HTTP requests")

    monkeypatch.setattr("httpx.AsyncClient.request", forbid_network)
    value = Lab(tmp_path)
    yield value
    await value.close()


async def current_plan(store):
    memory = await store.load()
    return memory.plans[memory.research_state.current_plan_id]


def main_requests(model):
    return [
        request
        for request in model.requests
        if {tool["name"] for tool in request.tools}.isdisjoint(
            {"submit_subagent_result", "submit_plan_proposal", "submit_plan_patch"}
        )
    ]


async def pause_after_research(lab, **options):
    bundle, store, model, fetch = await lab.create(pause=True, **options)
    events = []

    async def run():
        async for event in bundle.engine.submit_message(REQUEST):
            events.append(event)

    pending = lab.start(run())
    await await_point(model.paused, pending)
    return bundle, store, model, fetch, pending, events


async def test_e2e_01_complete_research_and_13_delivery(lab):
    bundle, store, model, fetch = await lab.create()
    events = await collect(bundle.engine)
    memory = await store.load()
    workspace = Path(memory.project.workspace_path)
    assert memory.project.status == "completed"
    assert all(
        (workspace / path).exists() for path in ["MEMORY.md", "artifacts", "reports", "subagents"]
    )
    assert len(model.planning_packets) == 1 and model.planning_packets[0]["plan"] is None
    assert all(task.status == "completed" for task in (await current_plan(store)).tasks)
    assert len(memory.conclusions) == 3 and len(model.checked(memory)) == 3
    policy = await CompletionPolicy().inspect_project(
        (await current_plan(store)), ResearchContext(memory, store)
    )
    assert policy.passed and not policy.missing_requirements
    draft = next(item for item in memory.artifacts.values() if item.kind == "report_draft")
    report = workspace / "reports" / f"{draft.id}.md"
    assert (
        report.is_file()
        and "# 研究摘要" in report.read_text()
        and "子代理整合" in report.read_text()
    )
    manifest, download = await SessionFiles(store).artifact(draft.file_id)
    assert manifest["status"] == "draft" and download.read_bytes() == report.read_bytes()
    assert draft.id in memory.project.delivery_manifest["artifacts"]
    assert all(item.execution_id and item.task_revision == 1 for item in memory.artifacts.values())
    calls = [event for event in events if isinstance(event, ToolExecutionCompleted)]
    assert len({event.tool_use_id for event in calls}) == len(calls) == len(model.calls) - 1
    assert {event.tool_name for event in calls} >= {
        "planner",
        "dispatch_subagents",
        "research_memory",
        "edit_file",
        "write_file",
    }
    assert (
        len(fetch.calls) == 6
    )  # Three children and the main agent's three original-source checks.
    assert bundle.engine.messages[-1].research_citations["citations"]
    assert bundle.engine.total_usage.input_tokens == len(model.requests) * 7


async def test_e2e_02_parallel_candidates_context_and_authority(lab):
    bundle, store, model, _ = await lab.create()
    await collect(bundle.engine)
    results = model.results[0]["results"]
    assert [item["task_id"] for item in results] == ["company", "industry", "financial"]
    assert all(item["status"] == "completed" for item in results) and model.child_peak == 3
    assert len({item["output_paths"][0] for item in results}) == 3
    requests = [
        request
        for request in model.requests
        if "submit_subagent_result" in {tool["name"] for tool in request.tools}
    ]
    for request in requests:
        packet = json.loads(request.messages[0].text)
        assert (
            packet["objective"]["subject"] == "AlphaTech" and packet["task_id"] in packet["context"]
        )
        for message in request.messages[1:]:
            if message.tool_uses:
                assert all(
                    call.name
                    not in {"planner", "replanner", "research_project", "dispatch_subagents"}
                    for call in message.tool_uses
                )
        for other in set(model.keys) - {packet["task_id"]}:
            assert f"{other} candidate:" not in " ".join(
                message.text for message in request.messages
            )
    denied = [
        block
        for request in requests
        for message in request.messages
        for block in message.content
        if isinstance(block, ToolResultBlock) and block.is_error
    ]
    assert any("main MEMORY.md is read-only" in block.content for block in denied)
    assert any(
        block.result_metadata.get("error_code") == "subagent_authority_denied" for block in denied
    )
    assert (
        "CHILD_FORBIDDEN"
        not in (Path((await store.load()).project.workspace_path) / "MEMORY.md").read_text()
    )
    assert not any(
        entry["data"].get("operation", {}).get("action") == "update_task"
        for entry in (await store.load()).history
    )


async def test_e2e_03_failed_child_preserves_siblings_and_main_continues(lab):
    bundle, store, model, _ = await lab.create(fail=["industry"])
    await collect(bundle.engine)
    results = model.results[0]["results"]
    assert [item["status"] for item in results] == ["completed", "failed", "completed"]
    assert "Expected offline child failure: industry" in results[1]["errors"][0]
    root = Path((await store.load()).project.workspace_path)
    assert all((root / item["output_paths"][0]).is_file() for item in (results[0], results[2]))
    assert (
        await store.load()
    ).project.status == "completed"  # Main repaired the gap by checking the original fixture.
    assert len(list(root.glob("subagents/*/industry/candidate.md"))) == 0


async def test_e2e_04_observable_concurrency_limit_and_queue(lab):
    bundle, _, model, _ = await lab.create(
        keys=["company", "industry", "financial", "four", "five"], concurrency=2
    )
    await collect(bundle.engine)
    assert model.child_peak == 2 and model.child_active == 0
    assert len(model.child_calls) == 5 and all(count == 6 for count in model.child_calls.values())
    assert len(model.results[0]["results"]) == 5
    assert all(item["status"] == "completed" for item in model.results[0]["results"])


async def test_e2e_05_bounded_recall_and_07_structured_provenance(lab):
    bundle, store, model, _ = await lab.create()
    await collect(bundle.engine)
    requests = main_requests(model)
    assert requests
    for request in requests:
        text = "\n".join(message.runtime_context or "" for message in request.messages)
        assert text.count("<research_memory>") == 1
        packet = workspace_packet(request)
        assert packet["project_id"] in {None, "Alpha"}
        assert "文件：artifacts/research-1.md" not in text
        assert "<!-- 记录重要发现" not in text
    assert workspace_packet(requests[-1])["project_id"] == "Alpha"
    memory = await store.load()
    markdown = (Path(memory.project.workspace_path) / "MEMORY.md").read_text()
    for evidence_id in model.checked(memory):
        evidence = memory.evidence_pool[evidence_id]
        assert evidence_id in markdown and (
            await store.read_source(memory.sources[evidence.source_id])
        ).startswith("合成")
        assert any(
            evidence_id in conclusion.evidence_ids for conclusion in memory.conclusions.values()
        )
    assert "摘要尚待交付" in markdown and memory.project.status == "completed"
    # The note remains on disk for explicit reads and never enters automatic recall.
    assert "文件：artifacts/research-1.md" in markdown


async def test_e2e_08_user_feedback_replanner_preserves_completed_task(lab):
    bundle, store, model, fetch, pending, events = await pause_after_research(lab)
    old_plan = await current_plan(store)
    prior_artifacts = list(old_plan.tasks[0].artifact_ids)
    runtime = bundle.engine.tool_metadata["research_runtime"]
    await runtime.submit_feedback("Alpha", FEEDBACK)
    model.release_main.set()
    await asyncio.wait_for(pending, timeout=60)
    assert not [
        event for event in events if isinstance(event, ToolExecutionCompleted) and event.is_error
    ]
    plan = await current_plan(store)
    assert plan.revision == 2 and plan.objective_revision == 2
    assert plan.tasks[0].status == "completed" and plan.tasks[0].artifact_ids == prior_artifacts
    draft = next(task for task in plan.tasks if task.id == "draft")
    assert draft.task_revision == 2 and draft.dependency_revisions == {"research": 1, "overseas": 1}
    packet = model.planning_packets[-1]
    assert packet["objective"]["revision"] == 2 and FEEDBACK in packet["feedback"]
    assert packet["plan"]["revision"] == 1 and packet["artifacts"]
    assert FEEDBACK in " ".join(
        message.runtime_context or "" for message in main_requests(model)[-1].messages
    )
    assert len([call for call in fetch.calls if call[0] == "company" and not call[2]]) == 1
    assert (await store.load()).project.status == "completed"


async def test_e2e_15_interrupt_and_16_restore_without_repeating_valid_work(lab):
    bundle, store, model, _, pending, _ = await pause_after_research(lab)
    completed = (await current_plan(store)).tasks[0].model_dump()
    web_sources = {
        source.id: source.model_dump()
        for source in (await store.load()).sources.values()
        if source.kind == "web"
    }
    root = Path((await store.load()).project.workspace_path)
    markdown = (root / "MEMORY.md").read_bytes()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert (await store.load()).project.status == "suspended" and (await current_plan(store)).tasks[
        0
    ].status == "completed"
    snapshot = [message.model_dump(mode="json") for message in bundle.engine.messages]
    resumed, restored, fresh_model, fetch = await lab.create(restore=snapshot)
    await collect(resumed.engine, "恢复并完成 AlphaTech 研究摘要")
    assert (await restored.load()).project.status == "completed" and Path(
        (await restored.load()).project.workspace_path
    ) == root
    assert (await current_plan(restored)).tasks[0].model_dump() == completed
    assert (root / "MEMORY.md").read_bytes() == markdown
    assert not fetch.calls and not fresh_model.results and not fresh_model.planning_packets
    assert len((await restored.load()).artifacts) == 2
    assert {
        source.id: source.model_dump()
        for source in (await restored.load()).sources.values()
        if source.kind == "web"
    } == web_sources


@pytest.mark.parametrize(
    "damage", ["missing", "modified", "symlink", "download_missing", "download_modified"]
)
async def test_e2e_13_rejects_missing_or_corrupt_workspace_delivery(lab, damage):
    bundle, store, model, _, pending, _ = await pause_after_research(lab)
    # Pause again immediately after the draft task completes, before finalize.
    async_gate = asyncio.Event()
    continue_gate = asyncio.Event()
    original_stream = model.stream_message

    async def gated(request):
        if (await current_plan(store)).tasks[-1].status == "completed" and (
            await store.load()
        ).project.status != "completed":
            async_gate.set()
            await continue_gate.wait()
        async for event in original_stream(request):
            yield event

    model.stream_message = gated
    model.release_main.set()
    await await_point(async_gate, pending)
    memory = await store.load()
    draft = next(item for item in memory.artifacts.values() if item.kind == "report_draft")
    path = Path(memory.project.workspace_path) / "reports" / f"{draft.id}.md"
    if damage.startswith("download_"):
        from sqlalchemy import delete, update
        from researchx.storage import schema as tables
        from researchx.storage.content import persist_object
        from researchx.storage.database import current_database
        from researchx.storage.research_records import scope

        # Download paths are verified caches now. Corrupt the authoritative manifest
        # or its content reference, preserving the original delivery-integrity assertion.
        where = (
            scope(tables.file_records, store.workspace_id, store.session_id),
            tables.file_records.c.record_id == draft.file_id,
        )
        async with current_database().transaction() as db:
            if damage == "download_missing":
                await db.execute(delete(tables.file_records).where(*where))
            else:
                key = await persist_object(
                    current_database(), store.workspace_id, b"wrong download"
                )
                await db.execute(
                    update(tables.file_records)
                    .where(*where)
                    .values(content_hash=key.removeprefix("sha256:"))
                )
    elif damage == "missing":
        path.unlink()
    elif damage == "modified":
        path.write_text("wrong report", encoding="utf-8")
    else:
        path.unlink()
        path.symlink_to(store.directory / draft.snapshot)
    result = await CompletionPolicy().inspect_project(
        (await current_plan(store)), ResearchContext((await store.load()), store)
    )
    try:
        assert not result.passed and any(
            "delivery" in check.check_id for check in result.checks if not check.passed
        )
        final = await bundle.engine.tool_metadata["research_runtime"].finalize("Alpha")
        assert not final.passed and (await store.load()).project.status == "running"
    finally:
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending


async def test_e2e_15_continuation_cancellation_settles_children_and_state(lab):
    bundle, store, model, _ = await lab.create(hold=["company", "industry", "financial"])
    # Leave the real main history with a pending dispatch call; continue_pending is the Web recovery entrypoint.
    stream = bundle.engine.submit_message(REQUEST)
    async for event in stream:
        if (
            isinstance(event, AssistantTurnComplete)
            and event.message.tool_uses
            and event.message.tool_uses[0].name == "dispatch_subagents"
        ):
            break
    await stream.aclose()
    # Closing before execution leaves an explicit interrupted receipt. Retry the
    # assignment through a new model call rather than replaying the old tool ID.
    model.seen_results.add(bundle.engine.messages[-1].content[0].tool_use_id)
    model.queue.insert(0, "dispatch")
    pending = lab.start(_collect_continuation(bundle.engine))
    await await_point(model.child_waiting, pending)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert model.child_active == 0 and set(model.child_cancelled) == set(model.keys)
    assert (await store.load()).project.status == "suspended"
    assert (await current_plan(store)).tasks[0].status == "blocked"
    assert bundle.engine.has_pending_continuation()
    assert any(
        isinstance(block, ToolResultBlock) and "interrupted" in block.content.lower()
        for block in bundle.engine.messages[-1].content
    )


async def _collect_continuation(engine):
    return [event async for event in engine.continue_pending()]


async def test_full_journey_parallel_memory_replan_interrupt_restore_and_deliver(lab):
    bundle, store, model, _, pending, events = await pause_after_research(lab)
    research_before = (await current_plan(store)).tasks[0].model_dump()
    model.pause_on_task = "overseas"
    await bundle.engine.tool_metadata["research_runtime"].submit_feedback("Alpha", FEEDBACK)
    model.release_main.set()
    await await_point(model.task_paused, pending)
    assert (await current_plan(store)).revision == 2 and (
        await store.load()
    ).research_state.current_task_id == "overseas"
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert (await store.load()).project.status == "suspended"
    saved = [message.model_dump(mode="json") for message in bundle.engine.messages]
    restored, recovered, fresh_model, fetch = await lab.create(restore=saved)
    final_events = await collect(restored.engine, "恢复海外分析，并生成最终研究摘要")
    memory = await recovered.load()
    plan = await current_plan(recovered)
    assert memory.project.status == "completed" and plan.revision == 2
    research = next(task for task in plan.tasks if task.id == "research")
    assert research.artifact_ids == research_before["artifact_ids"] and research.task_revision == 1
    assert [key for key, _, child in fetch.calls if not child] == ["overseas"]
    assert not fresh_model.planning_packets and not fresh_model.results
    assert len(memory.artifacts) == 3 and len(model.checked(memory)) == 4
    assert FEEDBACK in (Path(memory.project.workspace_path) / "MEMORY.md").read_text()
    assert (
        await CompletionPolicy().inspect_project(plan, ResearchContext(memory, recovered))
    ).passed
    assert memory.project.delivery_manifest["objective_revision"] == 2
    assert memory.project.delivery_manifest["plan_revision"] == 2
    assert (
        len(
            [
                event
                for event in events + final_events
                if isinstance(event, ToolExecutionCompleted) and event.tool_name == "planner"
            ]
        )
        == 1
    )
    assert (
        len(
            [
                event
                for event in events + final_events
                if isinstance(event, ToolExecutionCompleted) and event.tool_name == "replanner"
            ]
        )
        == 1
    )
