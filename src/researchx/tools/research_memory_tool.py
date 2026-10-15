"""Structured updates to the current conversation's research memory."""

from __future__ import annotations
from pydantic import ModelWrapValidatorHandler

import json

from pydantic import Field, ValidationError, model_validator

from researchx.state.models import Record, ResearchOperation, SearchMemory
from researchx.state.errors import ResearchError
from researchx.tools.base import BaseTool, ToolExecutionContext, ToolResult

LEGACY_STATE_MIGRATION = {
    "set_context": "使用 research_project.feedback 修改当前目标；research_project.start 创建新项目。",
    "create_plan": "调用 planner 生成初始 PlanProposal，或调用 replanner 生成 PlanPatch；Runtime 会验证并提交。",
    "update_task": "使用 research_project.claim_task/transition_task 和 complete_task；TaskManager 与 CompletionPolicy 会验证状态转换。",
}


class ResearchMemoryInput(Record):
    operation: ResearchOperation = Field(
        description="检索和读取使用当前研究作用域；写入前读取当前版本并使用唯一 operation_id 和 expected_revision。"
    )

    @model_validator(mode="wrap")
    @classmethod
    def remove_empty_extra_operation_fields(
        cls, value: object, handler: ModelWrapValidatorHandler[ResearchMemoryInput]
    ) -> ResearchMemoryInput:
        """Tolerate empty, unsupported decoration without weakening record validation."""
        try:
            return handler(value)
        except ValidationError as exc:
            if not isinstance(value, dict) or not isinstance(value.get("operation"), dict):
                raise
            operation = value["operation"]
            errors = exc.errors()
            if not all(
                error["type"] == "extra_forbidden"
                and len(error["loc"]) == 3
                and error["loc"][0] == "operation"
                and error["loc"][1] == operation.get("action")
                and error.get("input") in ("", None)
                for error in errors
            ):
                raise
            # Leave the original tool call intact for hooks and audit history.
            normalized = dict(operation)
            for error in errors:
                normalized.pop(error["loc"][-1])
            return handler({**value, "operation": normalized})


class ResearchMemoryTool(BaseTool[ResearchMemoryInput]):
    name = "research_memory"
    contract = {
        "name": "research_memory",
        "source": "builtin",
        "effect": "local_write",
        "required_capabilities": ("research.write",),
        "resources_write": ("research.control",),
        "parallelism": "resources",
    }
    description = (
        "检索研究记录并维护证据、来源链、结论、争议和可审计的 Reasoning Chain。"
        "search 按关键词、类型、任务和状态分页返回索引；read(ids, include_content) 获取记录和来源快照。"
        "报告目标、计划和任务由 ResearchAgentRuntime 负责；使用 research_project、planner/replanner 和 CompletionPolicy。"
        "set_context/create_plan/update_task 仅是旧会话兼容操作，在报告项目中会被拒绝。"
        "MEMORY.md 由主 Agent 的文件工具维护。add_evidence 必须使用程序发放的 source_id。"
        "verify_evidence 会在来源、跨来源或计算核验后创建可审计的后继记录。"
        "原文位置使用 locator，核验说明使用 verification_note；source_id_note 不是支持的字段，只使用架构定义的字段。"
        "add_reasoning 记录方法和结果，绝不记录私有思维链；add_conclusion 关联证据和论证。"
        "使用 supersedes 修订证据或结论。add_conflict 记录双方并标记核心结论待审查。"
        "主 Agent 检查原始来源、登记论证并提交 submit_conflict_report；可选的 dispatch_subagents 只返回候选结果。"
        "resolve_conflict 原子提交主 Agent 单独复核的裁决和后继结论；reopen_conflict 记录变化的证据或复核请求。"
        "回答中使用 [E:ev_ID] 引用证据。"
    )
    input_model = ResearchMemoryInput

    def validation_error_message(self, error: Exception) -> str:
        if not isinstance(error, ValidationError):
            return super().validation_error_message(error)
        details = "; ".join(
            f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
            for item in error.errors(include_url=False, include_input=False)
        )
        schema = self.input_model.model_json_schema()
        actions = schema["properties"]["operation"]["discriminator"]["mapping"]
        allowed = []
        for item in error.errors():
            location = item["loc"]
            if len(location) > 1 and location[1] in actions:
                definition = actions[location[1]].rsplit("/", 1)[-1]
                fields = ", ".join(schema["$defs"][definition]["properties"])
                hint = f"{location[1]} accepts: {fields}."
                if hint not in allowed:
                    allowed.append(hint)
        return (
            f"research_memory 输入无效：{details}。"
            + " ".join(allowed)
            + "没有提交任何写入。请读取当前版本，修正字段后重试。"
            "source_id 必须是程序发放的来源 ID；核验说明请使用 verification_note。"
        )

    async def execute(
        self, arguments: ResearchMemoryInput, context: ToolExecutionContext
    ) -> ToolResult:
        store = context.metadata.get("research_store")
        if store is None:
            return ToolResult(
                output="Research memory is not enabled for this session", is_error=True
            )
        try:
            operation = arguments.operation
            if isinstance(operation, SearchMemory):
                maximum = int(context.metadata.get("query_max_limit", 100))
                default = int(context.metadata.get("query_default_limit", 20))
                if not 1 <= default <= maximum <= 100:
                    raise ResearchError("研究记录查询配置无效")
                if "limit" not in operation.model_fields_set:
                    operation = operation.model_copy(update={"limit": default})
                if operation.limit > maximum:
                    raise ResearchError(f"研究记录查询 limit 不得超过 {maximum}")
            if arguments.operation.action in LEGACY_STATE_MIGRATION and (
                await store.load()
            ).project is not None:
                return ToolResult(
                    output="Legacy plan/task writes cannot bypass Runtime. "
                    + LEGACY_STATE_MIGRATION[arguments.operation.action],
                    is_error=True,
                )
            receipt = await store.apply(
                operation.model_dump(mode="json"),
                budget=int(context.metadata.get("research_injection_budget", 6000)),
                model=getattr(context.metadata.get("query_context"), "model", ""),
                model_access=True,
            )
        except (ResearchError, ValueError) as exc:
            return ToolResult(output=str(exc), is_error=True)
        progress = (
            receipt["progress"] if "progress" in receipt
            else None if arguments.operation.action in {"search", "read"}
            else await store.progress()
        )
        detail = {
            "add_conflict": "已登记双方证据与争议",
            "submit_conflict_report": "核查报告已保存，等待主代理审查裁决",
            "reopen_conflict": "相关判断需要重新核查",
        }.get(arguments.operation.action)
        if arguments.operation.action == "resolve_conflict":
            detail = (
                "争议仍未决，证据缺口已保留"
                if arguments.operation.decision.outcome == "unresolved"
                else "裁决已提交，适用条件与证据已保存"
            )
        return ToolResult(
            output=json.dumps(receipt, ensure_ascii=False, sort_keys=True),
            metadata={
                "context_component": "memory"
                if arguments.operation.action in {"read", "search"}
                else "dynamic_context",
                **(
                    {"research_sources": receipt["source_ids"]}
                    if arguments.operation.action == "read" and "source_ids" in receipt
                    else {}
                ),
                **({"research_progress": progress} if progress is not None else {}),
                **({"detail": detail} if detail else {}),
            },
        )

    def is_read_only(self, arguments: ResearchMemoryInput) -> bool:
        # Session bookkeeping is authorized by the research runtime; it cannot write arbitrary files.
        return True

    def execution_contract(self, arguments: ResearchMemoryInput | None = None) -> object:
        if arguments is not None and arguments.operation.action in {"read", "search"}:
            return {
                **self.contract,
                "effect": "read_only",
                "resources_write": (),
                "resources_read": ("research.control",),
            }
        return self.contract
