"""Research application surface used by the generic harness.

The engine asks this module whether a turn or tool may proceed. It does not
learn financial statement types itself.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import RowMapping

from researchx.state.business_coverage import Profile, classify_request, evaluate_coverage
from researchx.state.models import new_id


class ContextSnapshot(dict[str, object]):
    pass


@dataclass(frozen=True)
class AdmissionResult:
    allowed: bool
    message: str = ""
    profile: Profile | None = None


class RunPolicy(Protocol):
    async def context_snapshot(self) -> ContextSnapshot: ...

    async def admit(self, operation: object) -> AdmissionResult: ...

    async def observe(self, receipt: object) -> None: ...

    async def inspect_stop(self) -> str: ...


class ResearchApplication:
    """Target, admission and completion checks shared by chat and report runs."""

    def __init__(self, store: object) -> None:
        from researchx.state.store import ResearchStore

        if not isinstance(store, ResearchStore):
            raise TypeError("ResearchApplication requires ResearchStore")
        self.store = store

    async def prepare_user_turn(
        self,
        text: str,
        selected: Profile | None,
        ask: Callable[[str], Awaitable[str]] | None,
    ) -> AdmissionResult:
        if classify_request(text) == "ambiguous" and selected is None:
            reply = ""
            if ask is not None:
                reply = await ask(
                    "请确认本次研究类型：回复“事件点评”或“财报点评”。必查清单由系统固定，Agent 不能代选。"
                )
            if str(reply).strip() == "事件点评":
                selected = "event_commentary"
            elif str(reply).strip() == "财报点评":
                selected = "financial_commentary"
            else:
                return AdmissionResult(False, "研究类型不明确，请由发起人确认事件点评或财报点评后重试。")
        coverage_profile: Profile = selected or classify_request(text)
        window = evaluate_coverage(await self.store.load(), coverage_profile, text)
        return AdmissionResult(True, profile=selected, message=window.model_dump_json())

    async def admit_tool(
        self,
        tool_name: str,
        *,
        project_mode: bool,
        guard: Callable[[str], Awaitable[str | None]] | None,
    ) -> AdmissionResult:
        if project_mode and guard is not None:
            defect = await guard(tool_name)
            if defect:
                return AdmissionResult(False, str(defect))
        if self.store is None or project_mode or tool_name in {"research_memory", "ask_user_question", "skill"}:
            return AdmissionResult(True)
        memory = await self.store.load()
        if memory.research_state.replan_required:
            return AdmissionResult(
                False,
                "研究需要新计划。请先使用 research_memory 更新上下文并调用 create_plan，再执行其他工具。",
            )
        plan = memory.plans.get(memory.research_state.current_plan_id or "")
        if plan and not memory.research_state.current_task_id:
            return AdmissionResult(
                False,
                "开始采集或计算前先启动当前研究任务：使用真实任务 ID 和 in_progress 状态调用 research_memory.update_task，"
                "然后执行任务并更新结果。不要在所有任务仍为 pending 时收集整个计划的资料。",
            )
        return AdmissionResult(True)

    async def get_control_view(self, project_id: str) -> dict[str, object]:
        from sqlalchemy import select

        from researchx.storage.database import current_database
        from researchx.storage.schema import entities, research_sessions, research_tasks

        research_plans = entities["plans"]

        store = self.store
        queries = 0
        async with current_database().transaction() as db:
            session = (
                await db.execute(
                    select(
                        research_sessions.c.research_state,
                        research_sessions.c.revision,
                    ).where(
                        research_sessions.c.workspace_id == store.workspace_id,
                        research_sessions.c.session_id == store.session_id,
                    )
                )
            ).mappings().one()
            queries += 1
            plans = (
                await db.execute(
                    select(
                        research_plans.c.record_id,
                        research_plans.c.revision,
                    ).where(
                        research_plans.c.workspace_id == store.workspace_id,
                        research_plans.c.session_id == store.session_id,
                        research_plans.c.project_id == project_id,
                    )
                )
            ).mappings().all()
            queries += 1
            plan_ids = [row["record_id"] for row in plans]
            tasks: list[RowMapping] = []
            if plan_ids:
                task_rows = await db.execute(
                    select(
                        research_tasks.c.plan_id,
                        research_tasks.c.record_id,
                        research_tasks.c.status,
                        research_tasks.c.revision,
                    ).where(
                        research_tasks.c.workspace_id == store.workspace_id,
                        research_tasks.c.session_id == store.session_id,
                        research_tasks.c.plan_id.in_(plan_ids),
                    )
                )
                tasks = list(task_rows.mappings().all())
                queries += 1
        state = session["research_state"] if isinstance(session["research_state"], dict) else {}
        return {
            "project_id": project_id,
            "revision": session["revision"],
            "epoch": state.get("epoch", session["revision"]),
            "goal": state.get("next_step", ""),
            "plans": [
                {"id": row["record_id"], "revision": row["revision"]}
                for row in plans
            ],
            "tasks": [dict(row) for row in tasks],
            "query_count": queries,
        }


def new_run_id() -> str:
    return new_id("run")
