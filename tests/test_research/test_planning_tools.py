"""Bounded Agent-as-Tool calls use the real registry and parent commit path."""

import asyncio
import json

import pytest

from researchx.api.client import ApiMessageCompleteEvent
from researchx.api.usage import UsageSnapshot
from researchx.config.settings import PermissionSettings
from researchx.engine.messages import ConversationMessage, TextBlock, ToolUseBlock
from researchx.engine.query import QueryContext, _execute_tool_call, run_query
from researchx.engine.query_engine import QueryEngine
from researchx.permissions.checker import PermissionChecker
from researchx.state.models import PlanProposal, Record
from researchx.state.runtime import ResearchAgentRuntime
from researchx.tools import create_research_tool_registry
from researchx.tools.base import BaseTool, ToolExecutionContext, ToolResult
from researchx.tools.planner_tool import planner_tool
from tests.test_research.test_report_runtime import project as project, task, commit, claim


class PlanningModel:
    def __init__(self, mode="valid"):
        self.mode, self.requests = mode, []

    async def stream_message(self, request):
        self.requests.append(request)
        if self.mode == "timeout":
            await asyncio.sleep(30)
        if self.mode == "api_error":
            raise RuntimeError("offline provider failure")
        name = request.tools[0]["name"]
        if name == "submit_plan_proposal":
            payload = PlanProposal(
                objective_revision=1,
                tasks=[task(), task("draft", dependencies=["sources"], kind="report_draft")],
                rationale="来源支持初稿",
            ).model_dump(mode="json")
        else:
            packet = json.loads(request.messages[0].text)
            payload = {
                "base_plan_revision": packet["plan"]["revision"],
                "objective_revision": packet["objective"]["revision"],
                "add_tasks": [task("storage", criterion="增加储能预测").model_dump(mode="json")],
                "reason": "新增储能研究，保留财务任务",
            }
        if self.mode == "recursive":
            name = "planner"
        elif self.mode == "invalid":
            payload = {"unexpected": "no schema"}
        elif self.mode == "cycle":
            payload["tasks"] = [
                task("a", dependencies=["b"]).model_dump(mode="json"),
                task("b", dependencies=["a"]).model_dump(mode="json"),
            ]
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(
                role="assistant",
                content=[ToolUseBlock(id="child-submit", name=name, input=payload)],
            ),
            usage=UsageSnapshot(
                input_tokens=50000 if self.mode == "tokens" else 10, output_tokens=10
            ),
        )


def context(repo, model, **metadata):
    checker = PermissionChecker(PermissionSettings())
    runtime = ResearchAgentRuntime(repo.store, permission_checker=checker)
    query = QueryContext(
        api_client=model,
        tool_registry=create_research_tool_registry(),
        permission_checker=checker,
        cwd=repo.store.directory,
        model="fixture-model",
        system_prompt="parent",
        max_tokens=4096,
        context_window_tokens=200000,
        tool_metadata={"research_store": repo.store, "research_runtime": runtime, **metadata},
    )
    return query


async def test_function_only_proposes_then_real_tool_loop_commits(project):
    model = PlanningModel()
    query = context(project, model)
    before = (await project.store.load()).revision
    proposal = await planner_tool(
        "公司财报点评",
        ToolExecutionContext(
            cwd=query.cwd, metadata={**query.tool_metadata, "query_context": query}
        ),
    )
    assert not proposal["committed"] and (await project.store.load()).revision == before
    result = await _execute_tool_call(
        query, "planner", "parent-plan", {"objective": "公司财报点评"}
    )
    assert not result.is_error and result.result_metadata["commit"]["committed"]
    assert (await project.store.load()).project.status == "running"
    assert {item["name"] for item in model.requests[0].tools} == {"submit_plan_proposal"}
    assert all(request.model == "fixture-model" for request in model.requests)
    assert "parent-plan" not in {item.origin_id for item in (await project.store.load()).sources.values()}


async def test_replanner_is_registered_and_commits_incremental_patch(project):
    (await commit(project))
    (await project.submit_feedback("project_A", "增加储能预测", (await project.store.load()).revision))
    query = context(project, PlanningModel())
    result = await _execute_tool_call(query, "replanner", "replan", {"reason": "增加储能预测"})
    assert not result.is_error
    plan = project._plan((await project.store.load()))
    assert plan.revision == 2 and {item.id for item in plan.tasks} == {
        "sources",
        "draft",
        "storage",
    }
    assert len((await project.store.load()).plans) == 2


@pytest.mark.parametrize(
    "mode,metadata,error",
    [
        ("recursive", {}, "forbidden"),
        ("invalid", {"planning_max_calls": 1}, "call budget"),
        ("cycle", {}, "cycle"),
        ("timeout", {"planning_timeout_seconds": 0.01}, "timeout"),
        ("tokens", {}, "token budget"),
        ("api_error", {}, "provider failure"),
    ],
)
async def test_child_failure_suspends_without_fabricated_commit(project, mode, metadata, error):
    query = context(project, PlanningModel(mode), **metadata)
    result = await _execute_tool_call(
        query, "planner", "failed-plan", {"objective": "公司财报点评"}
    )
    assert result.is_error and error in result.content
    memory = (await project.store.load())
    assert memory.project.status == "suspended" and not memory.plans
    assert memory.project.planning_calls == 1


async def test_planner_permission_denial_does_not_call_model_or_mutate(project):
    model = PlanningModel()
    query = context(project, model)
    query.permission_checker = PermissionChecker(PermissionSettings(denied_tools=["planner"]))
    before = (await project.store.load()).revision
    result = await _execute_tool_call(query, "planner", "denied", {"objective": "财报点评"})
    assert result.is_error and not model.requests and (await project.store.load()).revision == before


async def test_subagent_recursion_rejected_even_if_function_called_directly(project):
    query = context(project, PlanningModel())
    with pytest.raises(Exception, match="recursion"):
        await planner_tool(
            "财报",
            ToolExecutionContext(
                cwd=query.cwd,
                metadata={
                    "query_context": query,
                    "research_store": project.store,
                    "planning_child": True,
                },
            ),
        )


async def test_planning_cas_conflict_suspends_preserving_newer_state(project):
    class RacingModel(PlanningModel):
        async def stream_message(self, request):
            (await project.store.capture(origin_id="concurrent-source", kind="user", content="新资料"))
            async for event in super().stream_message(request):
                yield event

    result = await _execute_tool_call(
        context(project, RacingModel()), "planner", "cas", {"objective": "财报"}
    )
    assert result.is_error and "Revision conflict" in result.content
    assert (await project.store.load()).project.status == "suspended"
    assert any(
        source.origin_id == "concurrent-source" for source in (await project.store.load()).sources.values()
    )


async def test_no_plan_blocks_research_and_legacy_plan_write(project):
    query = context(project, PlanningModel())
    result = await _execute_tool_call(query, "web_fetch", "blocked", {"url": "https://example.org"})
    assert result.is_error and "planner" in result.content
    result = await _execute_tool_call(
        query,
        "research_memory",
        "bypass",
        {
            "operation": {
                "action": "create_plan",
                "title": "绕过",
                "tasks": ["绕过"],
                "expected_revision": (await project.store.load()).revision,
                "operation_id": "bypass",
            }
        },
    )
    assert result.is_error and not (await project.store.load()).plans


@pytest.mark.parametrize("direct", [False, True])
async def test_premature_llm_stop_is_bounded_and_never_completes(project, direct):
    class PrematureModel:
        def __init__(self):
            self.calls = 0

        async def stream_message(self, request):
            self.calls += 1
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(role="assistant", content=[TextBlock(text="已完成")]),
                usage=UsageSnapshot(),
            )

    (await commit(project))
    model = PrematureModel()
    agent = QueryEngine(
        api_client=model,
        tool_registry=create_research_tool_registry(),
        permission_checker=PermissionChecker(PermissionSettings()),
        cwd=project.store.directory,
        model="fixture",
        system_prompt="parent",
        max_turns=6,
        context_window_tokens=200000,
        tool_metadata={"research_store": project.store, "business_profile": "financial_commentary"},
    )
    if direct:
        query = context(project, model)
        query.tool_metadata.pop("research_runtime")
        query.max_turns = 6
        _ = [
            event
            async for event in run_query(query, [ConversationMessage.from_user_text("继续研报")])
        ]
    else:
        _ = [event async for event in agent.submit_message("继续研报")]
    assert model.calls == 3 and (await project.store.load()).project.status == "suspended"
    if not direct:
        assert "阶段性" in agent.messages[-1].text
    assert not (await project.store.load()).project.delivery_manifest


@pytest.mark.parametrize(
    "spec", [{"content": "无效来源类型", "kind": "unsupported"}, {"kind": "file"}]
)
async def test_malformed_tool_provenance_cancels_execution_without_current_sources(project, spec):
    class MalformedTool(BaseTool):
        name, description, input_model = "malformed_source", "Invalid connector fixture", Record

        def is_read_only(self, arguments):
            return True

        async def execute(self, arguments, context):
            return ToolResult(output="无效连接器结果", metadata={"research_source_specs": [spec]})

    (await commit(project))
    (await claim(project, "sources"))
    query = context(project, PlanningModel())
    query.tool_registry.register(MalformedTool())
    result = await _execute_tool_call(query, "malformed_source", "bad-source", {})
    assert result.is_error and "Tool result rejected" in result.content
    execution = (await project.store.load()).executions[result.result_metadata["execution_id"]]
    assert execution.status == "cancelled"
    assert not any(
        source.origin_id == "bad-source" for source in (await project.store.load()).sources.values()
    )
