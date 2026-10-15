"""Offline financial commentary through the original QueryEngine and real Tool Registry."""

import json
from decimal import Decimal
from pathlib import Path

from researchx.api.client import ApiMessageCompleteEvent
from researchx.api.usage import UsageSnapshot
from researchx.config.settings import PermissionSettings
from researchx.engine.messages import ConversationMessage, TextBlock, ToolUseBlock
from researchx.engine.query_engine import QueryEngine
from researchx.permissions.checker import PermissionChecker
from researchx.state.models import PlanProposal, Record, ResearchObjective, new_id
from researchx.state.store import ResearchStore
from researchx.tools import create_research_tool_registry
from researchx.tools.base import BaseTool, ToolResult
from researchx.workspace.session_files import SessionFiles
from tests.test_research.test_report_runtime import task

FIXTURE = Path(__file__).parents[1] / "fixtures" / "earnings_commentary.json"
DATA = json.loads(FIXTURE.read_text())
KEYS = ["sources", "normalize", "analysis", "draft", "delivery"]
KINDS = ["note", "dataset", "model", "report_draft", "note"]


class FixtureInput(Record):
    stage: str


class FinancialFixtureTool(BaseTool):
    name = "financial_fixture"
    description = "Read the synthetic offline earnings fixture and run reproducible calculations"
    input_model = FixtureInput

    def is_read_only(self, arguments):
        return True

    async def execute(self, arguments, context):
        if arguments.stage == "analysis":
            prior = Decimal(str(DATA["previous"]["revenue"]))
            current = Decimal(str(DATA["current"]["revenue"]))
            profit = Decimal(str(DATA["current"]["net_profit"]))
            result = {
                "revenue_growth_pct": float((current / prior - 1) * 100),
                "net_profit_margin_pct": float(profit / current * 100),
                "unit": DATA["unit"],
                "currency": DATA["currency"],
                "period": DATA["period"],
                "fixture_notice": DATA["fixture_notice"],
            }
            assert result["revenue_growth_pct"] == DATA["expected"]["revenue_growth_pct"]
            assert result["net_profit_margin_pct"] == DATA["expected"]["net_profit_margin_pct"]
            output, kind = json.dumps(result, ensure_ascii=False), "calculation"
        elif arguments.stage == "draft":
            memory = await context.metadata["research_store"].load()
            ev = next(
                item.id
                for item in reversed(list(memory.evidence_pool.values()))
                if item.status == "verified"
            )
            output = (
                f"# 摘要\n{DATA['fixture_notice']}\n营收同比增长20%。 [E:{ev}]\n"
                f"# 财务表现\nFY2025，CNY million，净利润率10%。 [E:{ev}]\n"
                "# 风险\n合成离线案例，不外推为真实公司预测。\n"
            )
            kind = "file"
        elif arguments.stage == "delivery":
            output, kind = "完成初稿章节、数字与引用检查，交付离线验收初稿。", "file"
        else:
            output, kind = json.dumps(DATA, ensure_ascii=False), "file"
        return ToolResult(
            output=output,
            metadata={
                "research_source_specs": [
                    {
                        "content": output,
                        "kind": kind,
                        "title": "离线合成财报验收资料",
                        "locator": f"fixture:earnings_commentary:{arguments.stage}",
                        "fragment": False,
                    }
                ]
            },
        )


class EarningsModel:
    """Deterministic protocol driver; computations and policy run in actual tools/repository."""

    def __init__(self, store):
        self.store, self.calls, self.child_calls = store, 0, 0
        self.queue, self.active = [], None
        self.requests = []

    async def mutation(self, action, **values):
        return {
            "action": action,
            "operation_id": new_id("op"),
            "expected_revision": (await self.store.load()).revision,
            **values,
        }

    def objective(self):
        return ResearchObjective(
            project_id="offline_earnings",
            report_type=DATA["report_type"],
            subject=DATA["subject"],
            requirements=KEYS,
            deliverables=["report_draft"],
            required_sections=DATA["required_sections"],
        )

    def proposal(self):
        tasks = [
            task(key, kind=KINDS[index], dependencies=[KEYS[index - 1]] if index else [])
            for index, key in enumerate(KEYS)
        ]
        return PlanProposal(
            objective_revision=1, tasks=tasks, rationale="离线财报点评的可追溯研究问题"
        )

    def active_execution(self, memory):
        return next(
            item
            for item in reversed(list(memory.executions.values()))
            if item.task_id == self.active and item.status == "committed"
        )

    def source_evidence(self, memory):
        return next(
            item.id
            for item in reversed(list(memory.evidence_pool.values()))
            if item.status == "source_checked"
        )

    def calc_evidence(self, memory):
        return next(
            item.id
            for item in reversed(list(memory.evidence_pool.values()))
            if item.status == "verified"
        )

    async def work_operation(self, step, memory, plan):
        execution = self.active_execution(memory) if step != "execute" else None
        if step == "execute":
            return "financial_fixture", {"stage": self.active}
        if step == "add_source":
            return "research_memory", {
                "operation": await self.mutation(
                    "add_evidence",
                    source_id=execution.source_ids[0],
                    statement="FY2025营收120、净利润12，CNY million",
                )
            }
        if step in {"source_check", "calc_check"}:
            ev = next(reversed(memory.evidence_pool))
            return "research_memory", {
                "operation": await self.mutation(
                    "add_reasoning",
                    evidence_ids=[ev],
                    method="逐项核对完整离线 fixture 和数值运算结果",
                    result="记录可复核",
                    output="通过原文/计算核对",
                    verification=True,
                )
            }
        if step == "verify_source":
            return "research_memory", {
                "operation": await self.mutation(
                    "verify_evidence",
                    evidence_id=next(reversed(memory.evidence_pool)),
                    level="source_checked",
                    method="source",
                    verification_step_id=next(reversed(memory.reasoning_chain)),
                    verification_note="核对完整合成财报数据与口径",
                )
            }
        if step == "calculation_step":
            return "research_memory", {
                "operation": await self.mutation(
                    "add_reasoning",
                    evidence_ids=[self.source_evidence(memory)],
                    method="FinancialFixtureTool: (120/100-1)*100; 12/120*100, Decimal",
                    result="20%; 10%",
                    output="营收同比20%，净利润率10%，与离线金样本一致",
                )
            }
        if step == "add_calculation":
            return "research_memory", {
                "operation": await self.mutation(
                    "add_evidence",
                    source_id=execution.source_ids[0],
                    statement="营收同比20%，净利润率10%",
                    input_evidence_ids=[self.source_evidence(memory)],
                    calculation_step_id=next(reversed(memory.reasoning_chain)),
                )
            }
        if step == "verify_calculation":
            return "research_memory", {
                "operation": await self.mutation(
                    "verify_evidence",
                    evidence_id=next(reversed(memory.evidence_pool)),
                    level="verified",
                    method="calculation",
                    verification_step_id=next(reversed(memory.reasoning_chain)),
                    verification_note="确定性计算，输入已核对原文",
                )
            }
        if step == "finding":
            return "research_memory", {
                "operation": await self.mutation(
                    "add_conclusion",
                    statement="离线样本营收同比20%，净利润率10%",
                    status="verified",
                    evidence_ids=[self.calc_evidence(memory)],
                    step_ids=[next(reversed(memory.reasoning_chain))],
                )
            }
        if step == "finding_step":
            return "research_memory", {
                "operation": await self.mutation(
                    "add_reasoning",
                    evidence_ids=[self.calc_evidence(memory)],
                    verification=True,
                    method="用已核验计算证据形成发现",
                    result="离线样本增长20%，利润率10%",
                    output="财报点评核心发现",
                )
            }
        if step == "artifact":
            ev = (
                self.calc_evidence(memory)
                if self.active in {"analysis", "draft", "delivery"}
                else self.source_evidence(memory)
            )
            fields = {
                "task_id": self.active,
                "task_revision": 1,
                "plan_revision": 1,
                "execution_id": execution.id,
                "title": self.active,
                "kind": KINDS[KEYS.index(self.active)],
                "criteria": [self.active],
                "evidence_ids": [ev],
                "content": (await self.store.read_source(memory.sources[execution.source_ids[0]])),
            }
            if self.active in {"normalize", "analysis"}:
                fields.update(unit=DATA["unit"], currency=DATA["currency"], period=DATA["period"])
            if self.active == "analysis":
                fields.update(
                    reproduction="FinancialFixtureTool.execute uses Decimal: growth=(current/prior-1)*100; margin=profit/current*100",
                    input_artifact_ids=next(
                        item.artifact_ids for item in plan.tasks if item.id == "normalize"
                    ),
                    assumption_evidence_ids=[self.source_evidence(memory)],
                    finding_ids=[next(reversed(memory.conclusions))],
                )
            if self.active == "draft":
                fields.update(
                    sections=DATA["required_sections"],
                    input_artifact_ids=next(
                        item.artifact_ids for item in plan.tasks if item.id == "analysis"
                    ),
                    finding_ids=[next(reversed(memory.conclusions))],
                )
            return "research_project", {
                "operation": await self.mutation("submit_artifact", artifact=fields)
            }
        if step == "complete":
            return "research_project", {
                "operation": await self.mutation("complete_task", task_id=self.active, task_revision=1)
            }
        raise AssertionError(step)

    async def stream_message(self, request):
        self.requests.append(request)
        if {item["name"] for item in request.tools} == {"submit_plan_proposal"}:
            self.child_calls += 1
            name, values = "submit_plan_proposal", self.proposal().model_dump(mode="json")
        else:
            memory = (await self.store.load())
            plan = memory.plans.get(memory.research_state.current_plan_id or "")
            if memory.project is None:
                user = next(
                    key for key in reversed(memory.sources) if memory.sources[key].kind == "user"
                )
                name, values = (
                    "research_project",
                    {
                        "operation": await self.mutation(
                            "start",
                            objective=self.objective().model_dump(mode="json"),
                            user_source_ids=[user],
                        )
                    },
                )
            elif plan is None:
                name, values = "planner", {"objective": DATA["subject"]}
            elif self.queue:
                name, values = await self.work_operation(self.queue.pop(0), memory, plan)
            elif any(item.status == "ready" for item in plan.tasks):
                item = next(item for item in plan.tasks if item.status == "ready")
                self.active = item.id
                self.queue = ["execute"]
                if item.id == "sources":
                    self.queue += ["add_source", "source_check", "verify_source"]
                if item.id == "analysis":
                    self.queue += [
                        "calculation_step",
                        "add_calculation",
                        "calc_check",
                        "verify_calculation",
                        "finding_step",
                        "finding",
                    ]
                self.queue += ["artifact", "complete"]
                name, values = (
                    "research_project",
                    {"operation": await self.mutation("claim_task", task_id=item.id, task_revision=1)},
                )
            elif memory.project.status != "completed":
                name, values = "research_project", {"operation": await self.mutation("finalize")}
            else:
                yield ApiMessageCompleteEvent(
                    message=ConversationMessage(
                        role="assistant",
                        content=[
                            TextBlock(
                                text=f"离线合成财报点评初稿已通过检查，营收增长20%。 [E:{self.calc_evidence(memory)}]"
                            )
                        ],
                    ),
                    usage=UsageSnapshot(),
                )
                return
        self.calls += 1
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(
                role="assistant",
                content=[ToolUseBlock(id=f"call_{self.calls}", name=name, input=values)],
            ),
            usage=UsageSnapshot(input_tokens=10, output_tokens=10),
        )


async def test_earnings_report_end_to_end_and_restart(tmp_path, monkeypatch):
    from researchx.state import source_policy
    from researchx.state.models import SourceUsage
    import researchx.services.execution.tool_execution as execution_boundary
    import researchx.state.repository as repository_boundary

    original_usage = source_policy.collected_usage

    def fixture_usage(tool_name, spec):
        if tool_name == "financial_fixture":
            return SourceUsage(category="public", provider="synthetic_fixture",
                               licence_status="pending", model_use_allowed=True,
                               internal_export_allowed=True, completeness="full")
        return original_usage(tool_name, spec)

    # The offline fixture emulates a trusted, licensed collection adapter;
    # source-policy tests separately assert that arbitrary tools cannot self-grant.
    monkeypatch.setattr(source_policy, "collected_usage", fixture_usage)
    monkeypatch.setattr(execution_boundary, "collected_usage", fixture_usage)
    monkeypatch.setattr(repository_boundary, "collected_usage", fixture_usage)
    store = ResearchStore(tmp_path, "d" * 12, root=tmp_path / "memory")
    registry = create_research_tool_registry()
    registry.register(FinancialFixtureTool())
    model = EarningsModel(store)
    agent = QueryEngine(
        api_client=model,
        tool_registry=registry,
        permission_checker=PermissionChecker(PermissionSettings()),
        cwd=tmp_path,
        model="offline-fixture",
        system_prompt="research",
        context_window_tokens=1000000,
        max_turns=60,
        tool_metadata={"research_store": store},
    )
    events = [event async for event in agent.submit_message("根据离线合成财报生成财报点评初稿")]
    errors = [event for event in events if getattr(event, "is_error", False)]
    assert not errors, [
        (getattr(event, "tool_name", ""), getattr(event, "output", "")) for event in errors
    ]
    memory = (await store.load())
    assert memory.project.status == "completed" and model.child_calls == 1
    plan = memory.plans[memory.research_state.current_plan_id]
    assert all(item.status == "completed" for item in plan.tasks)
    assert len(memory.artifacts) == 5 and len(memory.executions) == 5
    workspace = Path(memory.project.workspace_path)
    assert (workspace / "MEMORY.md").is_file()
    assert len(list((workspace / "reports").glob("*.md"))) == 1
    assert all(
        item.execution_id and item.task_revision == 1 and item.plan_revision == 1
        for item in memory.artifacts.values()
    )
    draft = next(item for item in memory.artifacts.values() if item.kind == "report_draft")
    manifest, path = (await SessionFiles(store).artifact(draft.file_id))
    assert "营收同比增长20%" in path.read_text() and "CNY million" in path.read_text()
    assert manifest["status"] == "draft" and draft.sections == DATA["required_sections"]
    assert memory.project.delivery_manifest["plan_revision"] == 1
    assert draft.id in memory.project.delivery_manifest["artifacts"]
    assert not (await store.completion_warning())
    restored = (await ResearchStore(tmp_path, store.session_id, root=tmp_path / "memory").load())
    assert restored.project.delivery_manifest == memory.project.delivery_manifest
    assert agent.messages[-1].research_citations["citations"]
