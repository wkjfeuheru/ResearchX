"""Ordinary research fan-out uses the real loop, ledger and PostgreSQL storage."""

import asyncio
import json

import httpx
import pytest

from researchx.engine.query import QueryContext, _execute_tool_call, _execute_tool_batch
from researchx.engine.stream_events import ResearchProgressEvent
from researchx.config.settings import PermissionSettings
from researchx.permissions.checker import PermissionChecker
from researchx.state.runtime import ResearchAgentRuntime
from researchx.state.store import ResearchStore
from researchx.state.dispatch_audit import read_batch, recover_dispatches
from researchx.storage.content import read_object
from researchx.storage.database import current_database
from researchx.tools import create_research_tool_registry
from researchx.tools.base import ToolExecutionContext
from researchx.tools.contracts import resolve_contract
from researchx.tools.dispatch_subagents_tool import dispatch_subagents, SubagentTask
from researchx.tools.web_fetch_tool import WebFetchTool
from researchx.tools.web_search_tool import WebSearchTool
from researchx.engine.messages import ToolUseBlock
from tests.test_research.test_dispatch_subagents import ChildModel
from tests.test_research.test_store import apply, plan


@pytest.fixture
async def ordinary(tmp_path):
    store = ResearchStore(tmp_path, "a" * 12, root=tmp_path / "state")
    await plan(store, "宁德时代最近新闻与点评")
    task = (await store.load()).plans[(await store.load()).research_state.current_plan_id].tasks[0]
    await apply(store, "update_task", task_id=task.id, status="in_progress")
    return ResearchAgentRuntime(store)


def child_context(runtime, model, **metadata):
    query = QueryContext(
        api_client=model,
        tool_registry=create_research_tool_registry(),
        permission_checker=PermissionChecker(PermissionSettings(allowed_tools=["write_file"])),
        cwd=runtime.store.directory,
        model="fixture",
        system_prompt="main",
        max_tokens=4096,
        context_window_tokens=200000,
        tool_metadata={"research_store": runtime.store, "research_runtime": runtime, **metadata},
    )
    return ToolExecutionContext(
        cwd=runtime.store.directory, metadata={**query.tool_metadata, "query_context": query}
    )


async def test_ordinary_three_branches_overlap_and_persist_real_progress(ordinary):
    model = ChildModel()
    model.concurrency_target = 3
    observed = []

    async def progress(value):
        observed.append(value)

    ctx = child_context(ordinary, model, tool_progress=progress)
    result = await dispatch_subagents(
        [
            SubagentTask(task_id=key, title=title, instruction=title)
            for key, title in [("news", "新闻"), ("disclosure", "公告"), ("analysts", "研报观点")]
        ],
        ctx,
    )
    assert model.peak == 3
    assert all(item.status == "completed" for item in result.results)
    assert (await ordinary.store.load()).project is None
    assert any(
        sum(b["status"] == "running" for b in p["dispatches"][0]["branches"]) == 3 for p in observed
    )
    latest = (await ordinary.store.progress())["dispatches"][0]
    assert all(b["status"] == "awaiting_review" and not b["reviewed"] for b in latest["branches"])
    assert latest["version"] > observed[0]["dispatches"][0]["version"]
    for item in result.results:
        assert ((await ordinary.resolve_dispatch_workspace()) / item.output_paths[0]).is_file()
    assert (
        await recover_dispatches(ordinary.store, None, await ordinary.resolve_dispatch_workspace())
        is False
    )
    assert (await read_batch(ordinary.store, result.dispatch_id))["status"] == "completed"


async def test_three_actual_web_reads_overlap_and_sources_reach_main(ordinary, monkeypatch):
    active = peak = 0
    entered = asyncio.Event()

    async def fetch(url, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 3:
            entered.set()
        await asyncio.wait_for(entered.wait(), 5)
        active -= 1
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            headers={"content-type": "text/html"},
            text=f"<html><title>公司公告</title><p>原文数据 {url}</p></html>",
        )

    monkeypatch.setattr("researchx.tools.web_fetch_tool.fetch_public_http_response", fetch)
    model = ChildModel(
        initial={
            key: ("web_fetch", {"url": f"https://www.sse.com.cn/{key}"}) for key in ["a", "b", "c"]
        }
    )
    ctx = child_context(ordinary, model)
    query = ctx.metadata["query_context"]
    result = await _execute_tool_call(
        query,
        "dispatch_subagents",
        "collect-news",
        {"tasks": [{"task_id": key, "instruction": key} for key in ["a", "b", "c"]]},
    )
    assert not result.is_error, result.content
    assert peak == 3
    payload, _ = json.JSONDecoder().raw_decode(result.content)
    assert len(result.result_metadata["research_sources"]) == 3
    memory = await ordinary.store.load()
    assert memory.evidence_pool == {}  # Candidates never self-certify.
    for child in payload["results"]:
        source = child["candidate_sources"][0]
        content = await read_object(
            current_database(), ordinary.store.workspace_id, source["content_ref"]
        )
        assert source["content_hash"] == source["content_ref"].removeprefix("sha256:")
        assert source["locator"].encode() in content and source["fragment"] is False
        assert source["collected_at"]
    captured = [memory.sources[key] for key in result.result_metadata["research_sources"]]
    assert all(
        source.task_id == memory.research_state.current_task_id and not source.fragment
        for source in captured
    )


async def test_ordinary_lease_revoked_during_approval_prevents_write(ordinary):
    async def approve(*args):
        await ordinary.store.stopped()
        return "accept"

    model = ChildModel()
    ctx = child_context(ordinary, model, edit_approval_prompt=approve)
    result = await dispatch_subagents([SubagentTask(task_id="one", instruction="采集")], ctx)
    assert result.results[0].status == "failed"
    assert not list((await ordinary.resolve_dispatch_workspace()).glob("subagents/*/one/note.md"))


async def test_live_branch_progress_drained_before_tool_finishes(ordinary):
    model = ChildModel(wait=["slow"])
    ctx = child_context(ordinary, model)
    stream = _execute_tool_batch(
        ctx.metadata["query_context"],
        [
            ToolUseBlock(
                id="dispatch",
                name="dispatch_subagents",
                input={"tasks": [{"task_id": "slow", "instruction": "slow"}]},
            )
        ],
    )
    try:
        async for event, _, result in stream:
            if isinstance(event, ResearchProgressEvent) and any(
                b["status"] == "running"
                for batch in event.progress["dispatches"]
                for b in batch["branches"]
            ):
                assert result is None
                break
        else:
            pytest.fail("No live progress while dispatch was running")
    finally:
        await stream.aclose()
    assert model.active == 0
    progress = await ordinary.store.progress()
    assert progress["dispatches"][0]["branches"][0]["status"] == "cancelled"


def test_web_contracts_are_explicit_parallel_reads():
    for tool in (WebSearchTool(), WebFetchTool()):
        contract = resolve_contract(tool)
        assert contract.effect == "read_only" and contract.parallelism == "resources"
        assert not contract.resources_write


async def test_ordinary_retry_only_failed_branch_and_direction_change_rejected(ordinary):
    from researchx.state.errors import ResearchError

    model = ChildModel(fail=["bad"])
    ctx = child_context(ordinary, model)
    tasks = [SubagentTask(task_id=key, instruction=key) for key in ["good", "bad"]]
    first = await dispatch_subagents(tasks, ctx)
    assert [item.status for item in first.results] == ["completed", "failed"]
    retry_model = ChildModel()
    retried = await dispatch_subagents([], child_context(ordinary, retry_model), first.dispatch_id)
    assert [item.task_id for item in retried.results] == ["bad"] and "good" not in retry_model.calls
    await ordinary.store.stopped()
    await plan(ordinary.store, "修改研究方向")
    with pytest.raises(ResearchError):
        await dispatch_subagents([], ctx, first.dispatch_id)


async def test_ordinary_child_cannot_read_outside_workspace_or_sibling(ordinary):
    workspace = await ordinary.resolve_dispatch_workspace()
    sibling = workspace / "subagents" / "old" / "other" / "private.txt"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("PRIVATE")
    outside = ordinary.store.directory.parent / "private.txt"
    outside.write_text("OUTSIDE")
    for path in [sibling, outside]:
        model = ChildModel(initial={"one": ("read_file", {"path": str(path)})})
        result = await dispatch_subagents(
            [SubagentTask(task_id="one", instruction="read")], child_context(ordinary, model)
        )
        from tests.test_research.test_dispatch_subagents import latest_result

        feedback = latest_result(model.requests[1])
        assert feedback.is_error
        assert "PRIVATE" not in feedback.content and "OUTSIDE" not in feedback.content
        assert (
            result.results[0].status == "completed"
        )  # Child may repair by submitting an empty candidate.


async def test_late_ordinary_dispatch_cannot_publish_sources_after_stop(ordinary, monkeypatch):
    from researchx.tools.dispatch_subagents_tool import DispatchSubagentsTool

    original = DispatchSubagentsTool.execute

    async def execute(tool, arguments, ctx):
        result = await original(tool, arguments, ctx)
        await ordinary.store.stopped()
        return result

    async def fetch(url, **kwargs):
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            headers={"content-type": "text/html"},
            text="<p>原文</p>",
        )

    monkeypatch.setattr("researchx.tools.web_fetch_tool.fetch_public_http_response", fetch)
    monkeypatch.setattr(DispatchSubagentsTool, "execute", execute)
    model = ChildModel(initial={"one": ("web_fetch", {"url": "https://example.com"})})
    ctx = child_context(ordinary, model)
    before = set((await ordinary.store.load()).sources)
    result = await _execute_tool_call(
        ctx.metadata["query_context"],
        "dispatch_subagents",
        "late",
        {"tasks": [{"task_id": "one", "instruction": "read"}]},
    )
    assert result.is_error and "revoked parent scope" in result.content
    assert set((await ordinary.store.load()).sources) == before


async def test_serial_parallel_comparison_preserves_calls_and_usage(ordinary):
    import time

    measurements = []
    for limit in [1, 3]:
        usage = []
        model = ChildModel(delay=0.1)
        model.concurrency_target = limit
        ctx = child_context(
            ordinary, model, subagent_max_concurrency=limit, account_subagent_usage=usage.append
        )
        started = time.perf_counter()
        result = await dispatch_subagents(
            [
                SubagentTask(task_id=key, instruction=key)
                for key in ["news", "announcements", "analysts"]
            ],
            ctx,
        )
        assert all(item.status == "completed" for item in result.results)
        assert model.peak == limit and len(model.requests) == 6
        measurements.append(
            {
                "concurrency": limit,
                "seconds": round(time.perf_counter() - started, 3),
                "model_calls": len(model.requests),
                "tokens": sum(item.total_tokens for item in usage),
                "peak": model.peak,
            }
        )
    assert measurements[0]["tokens"] == measurements[1]["tokens"] == 48
    print("PARALLEL_BENCHMARK=" + json.dumps(measurements))


async def test_child_scope_binding_cannot_be_mutated_by_a_tool(ordinary, monkeypatch):
    from researchx.tools.file_write_tool import FileWriteTool

    original = FileWriteTool.execute
    seen = []

    async def execute(tool, arguments, context):
        scope = context.metadata["subagent_scope"]
        with pytest.raises(TypeError):
            scope["lease_id"] = "forged"
        assert isinstance(scope["baseline"], tuple)
        seen.append(scope["lease_id"])
        return await original(tool, arguments, context)

    monkeypatch.setattr(FileWriteTool, "execute", execute)
    result = await dispatch_subagents(
        [SubagentTask(task_id="one", instruction="采集")], child_context(ordinary, ChildModel())
    )
    assert seen and result.results[0].status == "completed"


async def test_main_can_locate_ordinary_candidate_outputs_from_original_cwd(ordinary):
    from pathlib import Path

    ctx = child_context(ordinary, ChildModel())
    result = await dispatch_subagents([SubagentTask(task_id="one", instruction="采集")], ctx)
    query = ctx.metadata["query_context"]
    query.cwd = Path(ordinary.store.cwd)
    candidate = Path(result.workspace) / result.results[0].output_paths[0]
    read = await _execute_tool_call(
        query, "read_file", "review-candidate", {"path": str(candidate)}
    )
    assert not read.is_error and "candidate_one" in read.content
    assert not (await ordinary.store.load()).evidence_pool
    candidate.write_text("未经授权替换的内部资料", encoding="utf-8")
    changed = await _execute_tool_call(query, "read_file", "changed-candidate", {"path": str(candidate)})
    assert changed.is_error and "未经授权替换的内部资料" not in changed.content


async def test_existing_checked_inputs_can_finish_review_without_new_sources(ordinary):
    from tests.test_research.test_store import evidence, reasoning

    store = ordinary.store
    ev = await evidence(store, "已核对经营数据")
    step = await reasoning(store, [ev], verification=True)
    checked = (
        await apply(
            store,
            "verify_evidence",
            evidence_id=ev,
            level="source_checked",
            method="source",
            verification_step_id=step,
            verification_note="主代理核查原文",
        )
    )["evidence_id"]
    model = ChildModel(evidence_refs=[checked], outputs=[])
    result = await dispatch_subagents(
        [SubagentTask(task_id="analysis", instruction="用已有证据分析")],
        child_context(ordinary, model),
    )
    assert not result.results[0].candidate_sources
    assert (await store.progress())["dispatches"][0]["branches"][0]["reviewed"] is False
    parent = (await store.load()).research_state.current_task_id
    await apply(
        store,
        "update_task",
        task_id=parent,
        status="completed",
        completion_note="主代理已复核分析及其证据",
    )
    assert (await store.progress())["dispatches"][0]["branches"][0]["reviewed"] is True
