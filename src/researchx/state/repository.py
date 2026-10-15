"""One write authority over the existing ResearchStore lock, CAS and audit log."""

from __future__ import annotations
from typing import Callable, TYPE_CHECKING, cast
from researchx.engine.metadata import ExecutionLease
from researchx.state.models import ResearchMemory, TaskStatus, ArtifactSubmission
from researchx.state.source_specs import SourceSpec
from researchx.state.source_policy import collected_usage

if TYPE_CHECKING:
    from researchx.state.store import ResearchStore

import hashlib
import json
from pathlib import Path

from researchx.state.completion import CompletionPolicy, ResearchContext
from researchx.state.errors import ResearchError
from researchx.state.models import (
    PlanPatch,
    PlanProposal,
    ResearchArtifact,
    ResearchExecution,
    ResearchObjective,
    ResearchPlan,
    ResearchProject,
    SourceRecord,
    TaskContext,
    new_id,
    now,
)
from researchx.state.tasks import PlanPatchValidator, TaskManager, validate_dag
from collections.abc import Awaitable
from researchx.storage.filesystem import atomic_write_text


class ResearchRepository:
    def __init__(self, store: ResearchStore, policy: CompletionPolicy | None = None) -> None:
        self.store = store
        self.policy = policy or CompletionPolicy()

    async def load_project(self, project_id: str) -> ResearchMemory:
        memory = await self.store.load()
        self._project(memory, project_id)
        return memory

    @staticmethod
    def _project(memory: ResearchMemory, project_id: str | None = None) -> ResearchProject:
        if memory.project is None or (project_id and memory.project.id != project_id):
            raise ResearchError("Unknown research project in this session")
        return memory.project

    @staticmethod
    def _plan(memory: ResearchMemory) -> ResearchPlan:
        plan = memory.plans.get(memory.research_state.current_plan_id or "")
        if plan is None:
            raise ResearchError("No current research plan")
        return plan

    async def _mutate(
        self,
        action: str,
        data: dict[str, object],
        expected_revision: int | None,
        change: Callable[
            [ResearchMemory], dict[str, object] | None | Awaitable[dict[str, object] | None]
        ],
        operation_id: str | None = None,
    ) -> dict[str, object]:
        fingerprint = hashlib.sha256(
            json.dumps(
                {"action": action, "data": data, "expected_revision": expected_revision},
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        async with self.store.transaction():
            memory = await self.store._load()
            if operation_id and operation_id in memory.operations:
                previous = memory.operations[operation_id]
                if previous["fingerprint"] != fingerprint:
                    raise ResearchError("operation_id already used with different content")
                return dict(previous["receipt"])
            if expected_revision is not None and expected_revision != memory.revision:
                raise ResearchError(
                    f"Revision conflict: expected {expected_revision}, current {memory.revision}"
                )
            pending = change(memory)
            if pending is None or isinstance(pending, dict):
                result = pending or {}
            else:
                result = await pending or {}
            receipt = {"revision": memory.revision + 1, **result}
            if operation_id:
                memory.operations[operation_id] = {"fingerprint": fingerprint, "receipt": receipt}
            (await self.store._save(memory, action, {**data, "result": result}))
            return receipt

    async def start(
        self,
        objective: ResearchObjective,
        user_source_ids: list[str],
        expected_revision: int,
        *,
        operation_id: str | None = None,
        initialize_project: Callable[[ResearchProject, ResearchObjective], object] | None = None,
    ) -> dict[str, object]:
        if objective.revision != 1:
            raise ResearchError("Initial objective revision must be 1")

        def change(memory: ResearchMemory) -> dict[str, object]:
            if memory.project:
                raise ResearchError("A research project already exists in this session")
            self.store._require(user_source_ids, memory.sources, "user source")
            if not user_source_ids or any(
                memory.sources[key].kind != "user" for key in user_source_ids
            ):
                raise ResearchError("A project requires a persisted user objective")
            self.store._archive_plan(memory)
            context = TaskContext(
                goal=objective.subject,
                subjects=[objective.subject],
                constraints=objective.requirements,
                deliverables=objective.deliverables,
                user_source_ids=user_source_ids,
                supersedes=memory.current_context_id,
            )
            memory.task_context.append(context)
            memory.current_context_id = context.id
            memory.objectives[1] = objective
            memory.project = ResearchProject(id=objective.project_id, status="planning")
            if initialize_project is not None:
                initialize_project(memory.project, objective)
            memory.research_state.replan_required = False
            return {
                "project_id": objective.project_id,
                "status": "planning",
                **(
                    {"workspace_path": memory.project.workspace_path}
                    if memory.project.workspace_path
                    else {}
                ),
            }

        return await self._mutate(
            "project_start",
            {"objective": objective.model_dump(mode="json"), "user_source_ids": user_source_ids},
            expected_revision,
            change,
            operation_id,
        )

    async def reserve_planning(self, kind: str) -> dict[str, int]:
        """Called by the loop after ordinary permission/hook checks, never by the child."""
        async with self.store.transaction():
            memory = await self.store._load()
            project = self._project(memory)
            if project.status in {"suspended", "completed", "failed", "cancelled"}:
                raise ResearchError(f"Project is {project.status}; resume or revise it explicitly")
            if kind == "planner" and project.plan_revision:
                raise ResearchError("An existing plan requires replanner, not replacement planning")
            if kind == "replanner" and not project.plan_revision:
                raise ResearchError("Initial planning requires planner")
            if (
                project.planning_calls >= project.max_planning_calls
                or project.planning_tokens >= project.max_planning_tokens
            ):
                self.suspend_memory(memory, "Planning budget exhausted")
                (await self.store._save(memory, "planning_budget_exhausted", {}))
            else:
                project.planning_calls += 1
                project.status = "planning" if kind == "planner" else "replanning"
                if kind == "replanner":
                    memory.research_state.replan_required = True
                    self.revoke_executions(memory, "Replanning started")
                (await self.store._save(memory, "planning_attempt", {"kind": kind}))
                return {
                    "revision": memory.revision,
                    "epoch": project.execution_epoch,
                    "plan_revision": project.plan_revision,
                    "objective_revision": project.objective_revision,
                    "remaining_tokens": project.max_planning_tokens - project.planning_tokens,
                }
        raise ResearchError("Planning budget exhausted; project suspended")

    @staticmethod
    def revoke_executions(memory: ResearchMemory, reason: str) -> None:
        project = ResearchRepository._project(memory)
        project.execution_epoch += 1
        for execution in memory.executions.values():
            if execution.status == "running":
                execution.status, execution.note = "cancelled", reason
        plan = memory.plans.get(memory.research_state.current_plan_id or "")
        if plan:
            for task in plan.tasks:
                if task.status in {"in_progress", "validating"}:
                    task.status, task.lease_id, task.blocker = "blocked", None, reason
        memory.research_state.current_task_id = None

    @classmethod
    def suspend_memory(cls, memory: ResearchMemory, reason: str) -> None:
        cls.revoke_executions(memory, reason)
        cls._project(memory).status, cls._project(memory).last_error = "suspended", reason

    async def suspend(self, reason: str, *, tokens: int = 0) -> dict[str, object]:
        def change(memory: ResearchMemory) -> dict[str, object]:
            project = self._project(memory)
            project.planning_tokens += tokens
            if project.status not in {"completed", "cancelled", "failed"}:
                self.suspend_memory(memory, reason)
            return {"status": project.status, "reason": reason}

        return await self._mutate(
            "project_suspend", {"reason": reason, "tokens": tokens}, None, change
        )

    async def record_planning_rejection(self, reason: str, tokens: int) -> dict[str, object]:
        def change(memory: ResearchMemory) -> dict[str, object]:
            self._project(memory).planning_tokens += tokens
            return {"committed": False, "reason": reason}

        return await self._mutate(
            "planning_result_rejected", {"reason": reason, "tokens": tokens}, None, change
        )

    async def set_planning_budget(
        self, project_id: str, *, max_calls: int, max_tokens: int, expected_revision: int
    ) -> dict[str, object]:
        """Trusted host/admin API; deliberately unavailable to model tools."""

        def change(memory: ResearchMemory) -> dict[str, object]:
            project = self._project(memory, project_id)
            if max_calls <= project.planning_calls or max_tokens <= project.planning_tokens:
                raise ResearchError("New budget must exceed consumed calls and tokens")
            project.max_planning_calls, project.max_planning_tokens = max_calls, max_tokens
            return {"max_calls": max_calls, "max_tokens": max_tokens}

        return await self._mutate(
            "set_planning_budget",
            {"project_id": project_id, "max_calls": max_calls, "max_tokens": max_tokens},
            expected_revision,
            change,
        )

    async def resume(
        self,
        project_id: str,
        *,
        expected_revision: int | None = None,
        operation_id: str | None = None,
    ) -> dict[str, object]:
        def change(memory: ResearchMemory) -> dict[str, object]:
            project = self._project(memory, project_id)
            if project.status != "suspended":
                raise ResearchError("Only suspended projects may resume")
            if (
                project.planning_calls >= project.max_planning_calls
                or project.planning_tokens >= project.max_planning_tokens
            ):
                raise ResearchError(
                    "Planning budget exhausted; adjust the budget explicitly before resuming"
                )
            project.status = (
                "replanning"
                if memory.research_state.replan_required
                else "running"
                if project.plan_revision
                else "planning"
            )
            plan = memory.plans.get(memory.research_state.current_plan_id or "")
            if plan:
                for task in plan.tasks:
                    if task.status == "blocked" and task.blocker in {
                        "Execution interrupted",
                        "执行已停止",
                        "Process restarted",
                        "Replanning started",
                    }:
                        task.status, task.blocker = "pending", ""
                TaskManager.refresh(plan)
            return {"status": project.status}

        return await self._mutate(
            "project_resume", {"project_id": project_id}, expected_revision, change, operation_id
        )

    async def commit_plan(
        self,
        project_id: str,
        proposal: PlanProposal,
        expected_revision: int,
        *,
        tokens: int = 0,
        operation_id: str | None = None,
    ) -> dict[str, object]:
        proposal = PlanProposal.model_validate(proposal)
        validate_dag(proposal.tasks, proposal=True)

        def change(memory: ResearchMemory) -> dict[str, object]:
            project = self._project(memory, project_id)
            if project.plan_revision or project.status != "planning":
                raise ResearchError("Initial plan can only be committed while planning")
            if proposal.objective_revision != project.objective_revision:
                raise ResearchError("Objective revision conflict")
            if proposal.clarification_questions:
                raise ResearchError(
                    "Resolve planner clarification questions before committing a plan"
                )
            project.planning_tokens += tokens
            if project.planning_tokens > project.max_planning_tokens:
                raise ResearchError("Planning token budget exhausted")
            tasks = [task.model_copy(deep=True) for task in proposal.tasks]
            plan = ResearchPlan(
                context_id=memory.current_context_id or "",
                project_id=project.id,
                objective_revision=project.objective_revision,
                revision=1,
                title=memory.objectives[project.objective_revision].subject[:120],
                tasks=tasks,
                rationale=proposal.rationale,
                assumptions=proposal.assumptions,
            )
            TaskManager.refresh(plan)
            memory.plans[plan.id] = plan
            memory.research_state.current_plan_id = plan.id
            project.plan_revision, project.status = 1, "running"
            return {"plan_id": plan.id, "plan_revision": 1, "committed": True}

        return await self._mutate(
            "commit_plan", proposal.model_dump(mode="json"), expected_revision, change, operation_id
        )

    async def apply_plan_patch(
        self,
        project_id: str,
        patch: PlanPatch,
        expected_revision: int,
        *,
        tokens: int = 0,
        operation_id: str | None = None,
    ) -> dict[str, object]:
        patch = PlanPatch.model_validate(patch)

        def change(memory: ResearchMemory) -> dict[str, object]:
            project = self._project(memory, project_id)
            if project.status not in {"running", "replanning"}:
                raise ResearchError("Project cannot accept a patch in its current state")
            if patch.objective_revision != project.objective_revision:
                raise ResearchError("Objective revision conflict")
            old = self._plan(memory)
            effective_patch = patch.model_copy(deep=True)
            # Artifact lineage can link tasks even when their DAG omits that relation.
            affected_ids = {item.task_id for item in patch.revise_tasks} | set(
                patch.cancel_task_ids
            )
            invalidated = set(patch.invalidate_artifact_ids)
            current_versions = {task.id: task.task_revision for task in old.tasks}
            invalidated |= {
                key
                for key, item in memory.artifacts.items()
                if item.stale and current_versions.get(item.task_id) == item.task_revision
            }
            while True:
                next_invalidated = invalidated | {
                    key
                    for key, item in memory.artifacts.items()
                    if item.task_id in affected_ids or set(item.input_artifact_ids) & invalidated
                }
                next_affected = (
                    affected_ids
                    | {
                        memory.artifacts[key].task_id
                        for key in next_invalidated
                        if key in memory.artifacts
                        and current_versions.get(memory.artifacts[key].task_id)
                        == memory.artifacts[key].task_revision
                    }
                    | {task.id for task in old.tasks if set(task.dependencies) & affected_ids}
                )
                if next_invalidated == invalidated and next_affected == affected_ids:
                    break
                invalidated, affected_ids = next_invalidated, next_affected
            effective_patch.invalidate_artifact_ids = sorted(
                key
                for key in invalidated
                if key in memory.artifacts
                and current_versions.get(memory.artifacts[key].task_id)
                == memory.artifacts[key].task_revision
            )
            tasks, affected = PlanPatchValidator().apply(old, effective_patch, memory.artifacts)
            project.planning_tokens += tokens
            if project.planning_tokens > project.max_planning_tokens:
                raise ResearchError("Planning token budget exhausted")
            stale = set(patch.invalidate_artifact_ids)
            stale |= {
                key for key, artifact in memory.artifacts.items() if artifact.task_id in affected
            }
            while True:
                expanded = stale | {
                    key
                    for key, item in memory.artifacts.items()
                    if set(item.input_artifact_ids) & stale
                }
                if expanded == stale:
                    break
                stale = expanded
            for key in stale:
                memory.artifacts[key].stale = True
                memory.artifacts[key].stale_reason = patch.reason
            # Preserve source facts. Findings based on revised analysis need review.
            for task in old.tasks:
                if task.id in affected:
                    for key in task.finding_ids:
                        if key in memory.conclusions:
                            memory.conclusions[key].needs_review = True
            plan = ResearchPlan(
                context_id=memory.current_context_id or "",
                project_id=project.id,
                objective_revision=project.objective_revision,
                revision=old.revision + 1,
                title=old.title,
                tasks=tasks,
                supersedes=old.id,
                rationale=patch.reason,
                reused_evidence_ids=(
                    list(
                        dict.fromkeys(
                            old.reused_evidence_ids
                            + [
                                key
                                for key, item in memory.evidence_pool.items()
                                if (item.plan_id == old.id or key in old.reused_evidence_ids)
                                if self.policy.evidence_valid(key, memory)
                            ]
                        )
                    )
                ),
            )
            old.archived = True
            self.revoke_executions(memory, "Plan revision changed")
            # revoke_executions only changes the old active lease; its completed history stays intact.
            memory.plans[plan.id] = plan
            memory.research_state.current_plan_id = plan.id
            memory.research_state.replan_required = False
            project.plan_revision, project.status = plan.revision, "running"
            project.delivery_manifest = {}
            TaskManager.refresh(plan)
            return {
                "plan_id": plan.id,
                "plan_revision": plan.revision,
                "committed": True,
                "revised_task_ids": sorted(affected),
                "stale_artifact_ids": sorted(stale),
            }

        return await self._mutate(
            "apply_plan_patch",
            patch.model_dump(mode="json"),
            expected_revision,
            change,
            operation_id,
        )

    async def transition_task(
        self,
        task_id: str,
        from_status: TaskStatus,
        to_status: TaskStatus,
        expected_revision: int,
        *,
        task_revision: int,
        blocker: str = "",
        operation_id: str | None = None,
    ) -> dict[str, object]:
        if to_status in {"completed", "validating"}:
            raise ResearchError("Task completion must pass CompletionPolicy")

        def change(memory: ResearchMemory) -> dict[str, object]:
            project = self._project(memory)
            if project.status != "running" or memory.research_state.replan_required:
                raise ResearchError("Project is not ready for task execution")
            plan = self._plan(memory)
            task = TaskManager.get(plan, task_id)
            if task.task_revision != task_revision:
                raise ResearchError("Task revision conflict")
            TaskManager.refresh(plan)
            if to_status in {"ready", "in_progress"} and not all(
                TaskManager.get(plan, key).status == "completed" for key in task.dependencies
            ):
                raise ResearchError("Task dependencies are unfinished")
            if to_status == "in_progress" and memory.research_state.current_task_id:
                raise ResearchError("Only one active task lease is allowed")
            if to_status == "blocked" and not blocker.strip():
                raise ResearchError("Blocking a task requires a blocker")
            TaskManager.transition(task, from_status, to_status)
            if to_status == "in_progress":
                memory.research_state.current_task_id = task.id
            elif memory.research_state.current_task_id == task.id:
                memory.research_state.current_task_id = None
                for execution in memory.executions.values():
                    if execution.task_id == task.id and execution.status == "running":
                        execution.status, execution.note = "cancelled", "Task lease released"
            task.blocker = blocker
            return {
                "task_id": task.id,
                "task_revision": task.task_revision,
                "status": task.status,
                "lease_id": task.lease_id,
            }

        return await self._mutate(
            "transition_task",
            {
                "task_id": task_id,
                "from": from_status,
                "to": to_status,
                "task_revision": task_revision,
                "blocker": blocker,
            },
            expected_revision,
            change,
            operation_id,
        )

    async def begin_execution(self, tool_name: str, tool_use_id: str) -> ExecutionLease:
        def change(memory: ResearchMemory) -> dict[str, object]:
            project = self._project(memory)
            task = TaskManager.get(self._plan(memory), memory.research_state.current_task_id or "")
            if project.status != "running" or task.status != "in_progress" or not task.lease_id:
                raise ResearchError("Claim a ready task before executing research tools")
            execution = ResearchExecution(
                id=new_id("exec"),
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                task_id=task.id,
                task_revision=task.task_revision,
                plan_revision=project.plan_revision,
                objective_revision=project.objective_revision,
                epoch=project.execution_epoch,
                lease_id=task.lease_id,
            )
            memory.executions[execution.id] = execution
            return {"execution": execution.model_dump(mode="json")}

        return cast(
            ExecutionLease,
            (
                await self._mutate(
                    "begin_execution",
                    {"tool_name": tool_name, "tool_use_id": tool_use_id},
                    None,
                    change,
                )
            )["execution"],
        )

    def execution_valid(self, memory: ResearchMemory, execution: ResearchExecution) -> bool:
        project = memory.project
        plan = memory.plans.get(memory.research_state.current_plan_id or "")
        task = (
            next((item for item in plan.tasks if item.id == execution.task_id), None)
            if plan
            else None
        )
        return bool(
            project
            and project.status == "running"
            and not memory.research_state.replan_required
            and execution.status == "running"
            and project.execution_epoch == execution.epoch
            and project.plan_revision == execution.plan_revision
            and project.objective_revision == execution.objective_revision
            and task
            and task.task_revision == execution.task_revision
            and task.lease_id == execution.lease_id
            and task.status == "in_progress"
        )

    async def commit_execution(
        self, execution_id: str, source_specs: list[SourceSpec], *, is_error: bool = False
    ) -> dict[str, object]:
        async def change(memory: ResearchMemory) -> dict[str, object]:
            execution = memory.executions[execution_id]
            if execution.status in {"committed", "failed"}:
                if (
                    len(execution.source_ids) != len(source_specs)
                    or any(
                        memory.sources[key].content_hash
                        != hashlib.sha256(spec["content"].encode()).hexdigest()
                        for key, spec in zip(execution.source_ids, source_specs)
                    )
                    or is_error != (execution.status == "failed")
                ):
                    raise ResearchError("Execution result already committed with different content")
                return {
                    "accepted": True,
                    "execution_id": execution_id,
                    "research_sources": execution.source_ids,
                    "task_revision": execution.task_revision,
                    "plan_revision": execution.plan_revision,
                }
            if not self.execution_valid(memory, execution):
                execution.status, execution.note = (
                    "rejected",
                    "Late result from a revoked execution version",
                )
                return {"accepted": False, "execution_id": execution_id, "research_sources": []}
            source_ids = []
            for index, spec in enumerate(source_specs):
                source_id = (
                    "src_"
                    + hashlib.sha256(f"{execution.tool_use_id}:{index}".encode()).hexdigest()[:20]
                )
                if source_id in memory.sources:
                    raise ResearchError("Tool call origin already committed")
                digest, snapshot = await self._snapshot(spec["content"])
                source = SourceRecord(
                    id=source_id,
                    plan_id=memory.research_state.current_plan_id,
                    task_id=execution.task_id,
                    kind=spec.get("kind", "tool"),
                    title=spec.get("title", execution.tool_name),
                    locator=spec.get(
                        "locator", f"tool:{execution.tool_name}:{execution.tool_use_id}"
                    ),
                    origin_id=execution.tool_use_id,
                    collected_at=now(),
                    published_at=spec.get("published_at"),
                    event_at=spec.get("event_at"),
                    content_hash=digest,
                    snapshot=snapshot,
                    fragment=spec.get("fragment", False),
                    is_error=is_error,
                    execution_id=execution.id,
                    task_revision=execution.task_revision,
                    plan_revision=execution.plan_revision,
                    usage=collected_usage(execution.tool_name, spec),
                    original_locator=spec.get("original_locator"),
                    corrects_source_id=spec.get("corrects_source_id"),
                )
                memory.sources[source.id] = source
                source_ids.append(source.id)
            execution.source_ids = source_ids
            execution.status = "failed" if is_error else "committed"
            task = TaskManager.get(self._plan(memory), execution.task_id)
            if is_error:
                task.failure_count += 1
            return {
                "accepted": True,
                "execution_id": execution_id,
                "research_sources": source_ids,
                "task_revision": execution.task_revision,
                "plan_revision": execution.plan_revision,
            }

        return await self._mutate(
            "commit_execution", {"execution_id": execution_id, "is_error": is_error}, None, change
        )

    async def cancel_execution(
        self, execution_id: str, note: str = "Execution interrupted"
    ) -> dict[str, object]:
        def change(memory: ResearchMemory) -> dict[str, object]:
            execution = memory.executions[execution_id]
            if execution.status == "running":
                execution.status, execution.note = "cancelled", note
            return {"execution_id": execution_id, "status": execution.status}

        return await self._mutate(
            "cancel_execution", {"execution_id": execution_id, "note": note}, None, change
        )

    async def _snapshot(self, content: str) -> tuple[str, str]:
        return await self.store.write_snapshot(content)

    async def submit_artifact(
        self, data: object, expected_revision: int, operation_id: str | None
    ) -> dict[str, object]:
        submission = ArtifactSubmission.model_validate(data)

        async def change(memory: ResearchMemory) -> dict[str, object]:
            project = self._project(memory)
            plan = self._plan(memory)
            task = TaskManager.get(plan, submission.task_id)
            if (
                project.status != "running"
                or task.status != "in_progress"
                or memory.research_state.replan_required
                or task.task_revision != submission.task_revision
                or plan.revision != submission.plan_revision
            ):
                raise ResearchError("Artifact execution version conflict")
            execution = memory.executions.get(submission.execution_id)
            if (
                not execution
                or execution.status != "committed"
                or execution.task_id != task.id
                or execution.task_revision != task.task_revision
                or execution.plan_revision != plan.revision
                or execution.epoch != project.execution_epoch
                or execution.lease_id != task.lease_id
            ):
                raise ResearchError("Artifact requires a successful execution of the active lease")
            self.store._require(submission.evidence_ids, memory.evidence_pool, "artifact evidence")
            self.store._require(
                submission.assumption_evidence_ids, memory.evidence_pool, "assumption evidence"
            )
            self.store._require(submission.finding_ids, memory.conclusions, "artifact finding")
            self.store._require(submission.input_artifact_ids, memory.artifacts, "input artifact")
            finding_evidence = [
                key
                for finding_id in submission.finding_ids
                for key in memory.conclusions[finding_id].evidence_ids
            ]
            finding_steps = [
                key
                for finding_id in submission.finding_ids
                for key in memory.conclusions[finding_id].step_ids
            ]
            self.store._ensure_scope(
                memory,
                submission.evidence_ids + submission.assumption_evidence_ids + finding_evidence,
                finding_steps,
            )
            if any(memory.artifacts[key].stale for key in submission.input_artifact_ids):
                raise ResearchError("Artifact cannot consume stale inputs")
            criteria = submission.criteria
            if not criteria or not set(criteria) <= set(task.acceptance_criteria):
                raise ResearchError("Bind artifact to explicit task acceptance criteria")
            digest, snapshot = await self._snapshot(submission.content)
            fields = submission.model_dump(exclude={"content", "criteria"})
            artifact = ResearchArtifact(
                **fields,
                snapshot=snapshot,
                content_hash=digest,
                objective_revision=project.objective_revision,
            )
            if artifact.kind == "report_draft":
                from researchx.workspace.session_files import SessionFiles

                path = self.store.directory / "drafts" / f"{artifact.id}.md"
                if project.workspace_path:
                    from researchx.state.runtime import ResearchAgentRuntime
                    from pathlib import Path

                    runtime = ResearchAgentRuntime(self.store)
                    root = runtime._workspace_path(project)
                    path = runtime._check_path(root, Path("reports") / f"{artifact.id}.md")
                atomic_write_text(path, submission.content, mode=0o600)
                manifest = await SessionFiles(self.store).register(
                    path,
                    task_id=task.id,
                    status="draft",
                    kind="report_draft",
                    execution=cast(ExecutionLease, execution.model_dump(mode="json")),
                )
                artifact.file_id = manifest["id"]
            memory.artifacts[artifact.id] = artifact
            task.artifact_ids.append(artifact.id)
            task.finding_ids = list(dict.fromkeys(task.finding_ids + artifact.finding_ids))
            for criterion in criteria:
                task.criterion_results.setdefault(criterion, []).append(artifact.id)
            return {"artifact_id": artifact.id, "file_id": artifact.file_id, "committed": True}

        receipt = await self._mutate(
            "submit_artifact",
            submission.model_dump(mode="json"),
            expected_revision,
            change,
            operation_id,
        )
        if submission.kind == "report_draft":
            from researchx.state.deliveries import DeliveryService

            memory = await self.store.load()
            objective = (
                memory.objectives.get(memory.project.objective_revision) if memory.project else None
            )
            request = (
                " ".join([objective.report_type, objective.subject, *objective.requirements])
                if objective
                else ""
            )
            await DeliveryService(self.store).freeze_report(str(receipt["artifact_id"]), request)
        return receipt

    async def request_task_completion(
        self,
        project_id: str,
        task_id: str,
        expected_revision: int,
        task_revision: int,
        *,
        operation_id: str | None = None,
    ) -> object:
        async def change(memory: ResearchMemory) -> dict[str, object]:
            project = self._project(memory, project_id)
            if project.status != "running" or memory.research_state.replan_required:
                raise ResearchError("Project cannot validate a task in its current state")
            plan = self._plan(memory)
            task = TaskManager.get(plan, task_id)
            if task.task_revision != task_revision:
                raise ResearchError("Task revision conflict")
            if any(
                item.status == "running"
                and item.task_id == task.id
                and item.lease_id == task.lease_id
                for item in memory.executions.values()
            ):
                raise ResearchError("Wait for active task executions to settle before validating")
            TaskManager.transition(task, "in_progress", "validating")
            result = await self.policy.inspect_task(task, ResearchContext(memory, self.store))
            TaskManager.transition(
                task, "validating", "completed" if result.passed else "in_progress"
            )
            task.defects = result.missing_requirements
            if result.passed:
                task.completion_note = "CompletionPolicy passed"
                memory.research_state.current_task_id = None
                TaskManager.refresh(plan)
            return {"completion": result.model_dump(mode="json"), "status": task.status}

        return (
            await self._mutate(
                "task_completion_check",
                {"task_id": task_id, "task_revision": task_revision},
                expected_revision,
                change,
                operation_id,
            )
        )["completion"]

    async def finalize(
        self, project_id: str, expected_revision: int, *, operation_id: str | None = None
    ) -> object:
        async def change(memory: ResearchMemory) -> dict[str, object]:
            project = self._project(memory, project_id)
            if project.status not in {"running", "completed"}:
                raise ResearchError("Project cannot finalize in its current state")
            plan = self._plan(memory)
            project.status = "validating"
            result = await self.policy.inspect_project(plan, ResearchContext(memory, self.store))
            project.status = "completed" if result.passed else "running"
            if result.passed:
                ids = [
                    key
                    for task in plan.tasks
                    if task.status == "completed"
                    for key in task.artifact_ids
                ]
                project.delivery_manifest = {
                    "plan_id": plan.id,
                    "plan_revision": plan.revision,
                    "objective_revision": project.objective_revision,
                    "artifacts": {
                        key: memory.artifacts[key].model_dump(mode="json") for key in ids
                    },
                    "evidence": {
                        key: memory.evidence_pool[key].model_dump(mode="json")
                        for artifact_id in ids
                        for key in memory.artifacts[artifact_id].evidence_ids
                    },
                    "completed_at": now(),
                }
            return {"completion": result.model_dump(mode="json"), "status": project.status}

        return (
            await self._mutate(
                "project_completion_check",
                {"project_id": project_id},
                expected_revision,
                change,
                operation_id,
            )
        )["completion"]

    async def recover(self) -> None:
        from researchx.state.dispatch_audit import recover_dispatches

        memory = await self.store.load()
        if memory.project and (
            await recover_dispatches(
                self.store,
                memory.project.id,
                Path(memory.project.workspace_path) if memory.project.workspace_path else None,
            )
        ):
            return  # Never revoke an execution whose dispatch owner still holds its lock.
        if memory.project and (
            any(item.status == "running" for item in memory.executions.values())
            or memory.research_state.current_task_id is not None
            or memory.project.status in {"planning", "replanning"}
            and memory.project.planning_calls
        ):
            (await self.suspend("Process restarted"))

    @classmethod
    def feedback_memory(cls, memory: ResearchMemory, text: str) -> None:
        project = cls._project(memory)
        if project.status in {"cancelled", "failed"}:
            raise ResearchError("A terminated project cannot accept feedback")
        previous = memory.objectives[project.objective_revision]
        objective = previous.model_copy(deep=True)
        objective.revision += 1
        objective.requirements = list(dict.fromkeys(objective.requirements + [text]))
        memory.objectives[objective.revision] = objective
        project.objective_revision = objective.revision
        project.feedback.append(text)
        cls.revoke_executions(memory, "Objective revised")
        project.status = "replanning" if project.plan_revision else "planning"
        memory.research_state.replan_required = bool(project.plan_revision)
        project.delivery_manifest = {}

    async def submit_feedback(
        self,
        project_id: str,
        feedback: str,
        expected_revision: int,
        *,
        operation_id: str | None = None,
    ) -> dict[str, object]:
        if not feedback.strip():
            raise ResearchError("Feedback must not be empty")

        def change(memory: ResearchMemory) -> dict[str, object]:
            self._project(memory, project_id)
            (self.feedback_memory(memory, feedback))
            return {
                "objective_revision": self._project(memory).objective_revision,
                "status": self._project(memory).status,
            }

        return await self._mutate(
            "project_feedback", {"feedback": feedback}, expected_revision, change, operation_id
        )

    async def cancel(
        self,
        project_id: str,
        reason: str,
        expected_revision: int,
        *,
        operation_id: str | None = None,
    ) -> dict[str, object]:
        def change(memory: ResearchMemory) -> dict[str, object]:
            project = self._project(memory, project_id)
            if project.status in {"completed", "cancelled", "failed"}:
                raise ResearchError("Project already has a terminal status")
            self.revoke_executions(memory, reason)
            plan = memory.plans.get(memory.research_state.current_plan_id or "")
            if plan:
                for task in plan.tasks:
                    if task.status not in {"completed", "failed", "cancelled"}:
                        task.status = "cancelled"
            project.status, project.last_error = "cancelled", reason
            return {"status": project.status}

        return await self._mutate(
            "project_cancel",
            {"project_id": project_id, "reason": reason},
            expected_revision,
            change,
            operation_id,
        )
