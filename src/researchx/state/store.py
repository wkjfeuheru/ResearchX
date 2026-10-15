"""Atomic per-session research storage, provenance, and bounded prompt views."""

from __future__ import annotations
from researchx.services.context.sources import ContextSnapshot
from collections.abc import Mapping
from researchx.state.models import (
    PendingSteer,
    SourceKind,
    CitationSnapshot,
    SourceDisplay,
)

import hashlib
import json
import re
from pathlib import Path
from typing import Any, cast, Literal
from urllib.parse import urlsplit

from pydantic import TypeAdapter

from researchx.config.paths import get_data_dir
from researchx.state.models import (
    AddConclusion,
    AddEvidence,
    AddReasoning,
    Conclusion,
    CreatePlan,
    Evidence,
    ReadMemory,
    SearchMemory,
    ReasoningStep,
    ResearchMemory,
    ResearchOperation,
    ResearchPlan,
    ResearchTask,
    SetContext,
    SourceRecord,
    SourceUsage,
    TaskContext,
    UpdateTask,
    VerifyEvidence,
    now,
    new_id,
)
from researchx.services.context.token_estimation import estimate_tokens
from researchx.storage.filesystem import private_directory
from researchx.storage.database import current_database
from typing import TYPE_CHECKING
from contextlib import AbstractAsyncContextManager
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from researchx.storage.research_records import ResearchRecords
from researchx.storage.content import ContentReference, ContentStore, content_store
from researchx.storage.schema import content_objects
from sqlalchemy import select
from researchx.config.sites import source_identity
from researchx.state.conflicts import ConflictStoreMixin
from researchx.state.errors import ResearchError

OPERATION_ADAPTER: TypeAdapter[ResearchOperation] = TypeAdapter(ResearchOperation)
CITATION_PATTERN = re.compile(r"\[E:([^\]\s]+)\]")


class ResearchStore(ConflictStoreMixin):
    def __init__(self, cwd: str | Path, session_id: str, *, root: Path | None = None) -> None:
        if not re.fullmatch(r"[a-f0-9]{12}", session_id):
            raise ResearchError("无效的研究会话 ID")
        digest = hashlib.sha256(str(Path(cwd).resolve()).encode()).hexdigest()[:16]
        self.session_id = session_id
        self.cwd = str(Path(cwd).resolve())
        self.workspace_id = digest
        self.directory = (root or get_data_dir() / "research" / digest) / session_id
        self.lock = self.directory / ".content.lock"
        private_directory(self.directory)

    @property
    def records(self) -> ResearchRecords:
        from researchx.storage.research_records import ResearchRecords

        return ResearchRecords(current_database(), self.workspace_id, self.session_id, self.cwd)

    def transaction(self) -> AbstractAsyncContextManager[AsyncSession]:
        return self.records.transaction()

    @property
    def content(self) -> ContentStore:
        return content_store()

    async def _load(self) -> ResearchMemory:
        try:
            memory = await self.records.load(current_database().active_session())
            self._validate(memory)
            await self._validate_content(memory)
            return memory
        except (ValueError, OSError, KeyError) as exc:
            raise ResearchError("研究状态损坏或资料快照缺失；数据已保留，请修复后继续") from exc

    async def load(self) -> ResearchMemory:
        # The relational view is loaded in one MVCC statement. Mutations still use
        # records.transaction() and its exclusive session lock.
        async with current_database().transaction():
            return await self._load()

    async def _save(self, memory: ResearchMemory, action: str, data: dict[str, object]) -> None:
        previous_revision = memory.revision
        await self._validate_content(memory)
        memory.revision += 1
        memory.history.append(
            {"revision": memory.revision, "action": action, "data": data, "at": now()}
        )
        await self.records.save(
            current_database().active_session(),
            memory,
            self._validate,
            expected_revision=previous_revision,
        )

    async def _content_reference(self, digest: str) -> ContentReference:
        async with current_database().transaction() as db:
            row = (
                (
                    await db.execute(
                        select(content_objects).where(
                            content_objects.c.workspace_id == self.workspace_id,
                            content_objects.c.content_hash == digest,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ResearchError("资料快照引用缺失")
            return ContentReference(**dict(row))

    async def _validate_content(self, memory: ResearchMemory) -> None:
        digests = {item.content_hash for item in memory.sources.values()} | {
            item.content_hash for item in memory.artifacts.values()
        }
        if not digests:
            return
        # Validate every reference, with one scoped SQL query instead of N network round trips.
        async with current_database().transaction() as db:
            rows = (
                (
                    await db.execute(
                        select(content_objects).where(
                            content_objects.c.workspace_id == self.workspace_id,
                            content_objects.c.content_hash.in_(digests),
                        )
                    )
                )
                .mappings()
                .all()
            )
        if {row["content_hash"] for row in rows} != digests:
            raise ResearchError("资料快照引用缺失")
        if not await self.content.exists_many([ContentReference(**dict(row)) for row in rows]):
            raise ResearchError("资料快照缺失；数据已保留")

    async def write_snapshot(self, content: str) -> tuple[str, str]:
        try:
            reference = await self.content.put(
                self.workspace_id, content.encode(), media_type="text/plain"
            )
        except (ValueError, OSError) as exc:
            raise ResearchError("资料快照校验失败；原内容已保留") from exc
        async with self.transaction() as db:
            await self.records.register_content(db, self.content, [reference])
        return reference.content_hash, f"content/{reference.content_hash}.txt"

    @staticmethod
    def _require(ids: list[str], records: Mapping[str, object], label: str) -> None:
        missing = [key for key in ids if key not in records]
        if missing:
            raise ResearchError(f"Unknown {label} IDs in this session: {', '.join(missing)}")

    def _validate(self, memory: ResearchMemory) -> None:
        contexts = {item.id: item for item in memory.task_context}
        if len(contexts) != len(memory.task_context):
            raise ResearchError("Duplicate context IDs")
        if memory.current_context_id:
            self._require([memory.current_context_id], contexts, "context")
        buckets = (
            contexts,
            memory.sources,
            memory.plans,
            memory.evidence_pool,
            memory.reasoning_chain,
            memory.conclusions,
            memory.conflicts,
            memory.arbitrations,
        )
        if sum(len(records) for records in buckets) != len(
            {key for records in buckets for key in records}
        ):
            raise ResearchError("Record IDs must be unique within the session")
        for records in buckets:
            if any(key != record.id for key, record in records.items()):
                raise ResearchError("Record ID mismatch")
        for records in (contexts, memory.plans, memory.evidence_pool, memory.conclusions):
            for record in records.values():
                visited = {record.id}
                previous = record.supersedes
                while previous:
                    self._require([previous], records, "superseded record")
                    if previous in visited:
                        raise ResearchError("Record history cannot contain a cycle")
                    visited.add(previous)
                    previous = records[previous].supersedes
        for source in memory.sources.values():
            if source.plan_id:
                self._require([source.plan_id], memory.plans, "source plan")
                if source.task_id and source.task_id not in {
                    task.id for task in memory.plans[source.plan_id].tasks
                }:
                    raise ResearchError("Source task does not belong to its plan")
            elif source.task_id:
                raise ResearchError("A source task requires a source plan")
            if source.snapshot != f"content/{source.content_hash}.txt" or not re.fullmatch(
                r"[a-f0-9]{64}", source.content_hash
            ):
                raise ResearchError("Invalid snapshot reference")
        for context in contexts.values():
            self._require(context.user_source_ids, memory.sources, "user source")
            if any(memory.sources[key].kind != "user" for key in context.user_source_ids):
                raise ResearchError("Task context must refer to user messages")
        for plan in memory.plans.values():
            self._require([plan.context_id], contexts, "context")
            self._require(plan.reused_evidence_ids, memory.evidence_pool, "evidence")
            if len({task.id for task in plan.tasks}) != len(plan.tasks):
                raise ResearchError("Duplicate task IDs")
            if sum(task.status == "in_progress" for task in plan.tasks) > 1:
                raise ResearchError("Only one research task may be in progress")
        state = memory.research_state
        if state.current_plan_id:
            self._require([state.current_plan_id], memory.plans, "plan")
            plan = memory.plans[state.current_plan_id]
            if plan.archived:
                raise ResearchError("Current plan is archived")
            if state.current_task_id and state.current_task_id not in {
                task.id for task in plan.tasks
            }:
                raise ResearchError("Current task does not belong to current plan")
            active = [task.id for task in plan.tasks if task.status == "in_progress"]
            if active != ([state.current_task_id] if state.current_task_id else []):
                raise ResearchError("Current task must match the in-progress task")
        for evidence in memory.evidence_pool.values():
            if evidence.plan_id:
                self._require([evidence.plan_id], memory.plans, "evidence plan")
                if evidence.task_id and evidence.task_id not in {
                    task.id for task in memory.plans[evidence.plan_id].tasks
                }:
                    raise ResearchError("Evidence task does not belong to its plan")
            elif evidence.task_id:
                raise ResearchError("An evidence task requires an evidence plan")
            self._require([evidence.source_id], memory.sources, "source")
            self._require(
                evidence.supporting_evidence_ids, memory.evidence_pool, "supporting evidence"
            )
            source = memory.sources[evidence.source_id]
            if source.is_error:
                raise ResearchError("Failed tool output cannot become evidence")
            if (
                evidence.collected_at != source.collected_at
                or evidence.published_at != source.published_at
            ):
                raise ResearchError("Evidence timestamps must come from its source")
            self._require(evidence.input_evidence_ids, memory.evidence_pool, "input evidence")
            if source.kind == "calculation":
                if not evidence.input_evidence_ids or not evidence.calculation_step_id:
                    raise ResearchError("Calculation evidence needs inputs and a calculation step")
                self._require(
                    [evidence.calculation_step_id], memory.reasoning_chain, "calculation step"
                )
                inputs = self._step_evidence(memory, [evidence.calculation_step_id])
                if not set(evidence.input_evidence_ids) <= inputs:
                    raise ResearchError("Calculation step must reference its input evidence")
            if evidence.status in {"source_checked", "verified"}:
                if not evidence.verification_step_id or not evidence.verification_note.strip():
                    raise ResearchError("Checked evidence needs an explicit verification record")
                self._require(
                    [evidence.verification_step_id], memory.reasoning_chain, "verification step"
                )
                step = memory.reasoning_chain[evidence.verification_step_id]
                inputs = self._step_evidence(memory, [step.id])
                if not step.verification or not any(
                    memory.evidence_pool[key].source_id == source.id for key in inputs
                ):
                    raise ResearchError("Verification must examine the evidence source")
                if evidence.status == "source_checked":
                    if evidence.verification_method != "source":
                        raise ResearchError("Source-checked evidence must use source verification")
                    if source.kind in {"search", "user"} or source.fragment:
                        raise ResearchError(
                            "Search snippets, user accounts and partial sources cannot be source-checked"
                        )
                elif evidence.verification_method == "cross_source":
                    if not evidence.supporting_evidence_ids:
                        raise ResearchError("Cross-source verification needs supporting evidence")
                    if not set(evidence.supporting_evidence_ids) <= inputs:
                        raise ResearchError("Verification step must reference supporting evidence")
                    candidates = [
                        memory.evidence_pool[key] for key in evidence.supporting_evidence_ids
                    ]
                    if any(
                        item.status not in {"source_checked", "verified"} or item.needs_review
                        for item in candidates
                    ):
                        raise ResearchError("Supporting evidence must already be source-checked")
                    identities = {
                        self._source_identity(memory.sources[item.source_id]) for item in candidates
                    }
                    identities.add(self._source_identity(source))
                    if len(identities) < 2:
                        raise ResearchError("Cross-source verification needs independent sources")
                elif evidence.verification_method == "calculation":
                    if source.kind != "calculation" or not evidence.input_evidence_ids:
                        raise ResearchError(
                            "Calculation verification needs calculation evidence and inputs"
                        )
                    if any(
                        memory.evidence_pool[key].status not in {"source_checked", "verified"}
                        or memory.evidence_pool[key].needs_review
                        for key in evidence.input_evidence_ids
                    ):
                        raise ResearchError("Calculation inputs must already be source-checked")
                else:
                    raise ResearchError(
                        "Verified evidence needs cross-source or calculation verification"
                    )
            if evidence.supersedes:
                self._require([evidence.supersedes], memory.evidence_pool, "superseded evidence")
        for step in memory.reasoning_chain.values():
            if step.plan_id:
                self._require([step.plan_id], memory.plans, "reasoning plan")
                if step.task_id and step.task_id not in {
                    task.id for task in memory.plans[step.plan_id].tasks
                }:
                    raise ResearchError("Reasoning task does not belong to its plan")
            elif step.task_id:
                raise ResearchError("A reasoning task requires a reasoning plan")
            self._require(step.evidence_ids, memory.evidence_pool, "evidence")
            self._require(step.prior_step_ids, memory.reasoning_chain, "reasoning step")
            self._step_evidence(memory, [step.id])
        for claim in memory.conclusions.values():
            if claim.plan_id:
                self._require([claim.plan_id], memory.plans, "conclusion plan")
            self._require(claim.evidence_ids, memory.evidence_pool, "evidence")
            self._require(claim.step_ids, memory.reasoning_chain, "reasoning step")
            if not set(claim.evidence_ids) <= self._step_evidence(memory, claim.step_ids):
                raise ResearchError("Conclusion evidence must be used by its reasoning steps")
            if claim.status == "verified" and not claim.needs_review:
                if any(
                    memory.evidence_pool[key].status != "verified"
                    or memory.evidence_pool[key].needs_review
                    for key in claim.evidence_ids
                ):
                    raise ResearchError("Verified conclusion needs verified evidence")
                if not any(memory.reasoning_chain[key].verification for key in claim.step_ids):
                    raise ResearchError("Verified conclusion needs a verification step")
            if claim.supersedes:
                self._require([claim.supersedes], memory.conclusions, "superseded conclusion")
        self._validate_conflicts(memory)
        if memory.project is not None:
            from researchx.state.tasks import validate_dag

            project = memory.project
            if project.objective_revision not in memory.objectives:
                raise ResearchError("Missing current objective revision")
            for revision, objective in memory.objectives.items():
                if revision != objective.revision or objective.project_id != project.id:
                    raise ResearchError("Objective version mismatch")
            for plan in memory.plans.values():
                if plan.project_id == project.id:
                    validate_dag(plan.tasks)
                    by_id = {task.id: task for task in plan.tasks}
                    for task in plan.tasks:
                        if task.dependency_revisions != {
                            key: by_id[key].task_revision for key in task.dependencies
                        }:
                            raise ResearchError(
                                "Task dependency revisions must be pinned within the plan"
                            )
            if (
                state.current_plan_id
                and memory.plans[state.current_plan_id].revision != project.plan_revision
            ):
                raise ResearchError("Project plan revision mismatch")
            for artifact in memory.artifacts.values():
                if artifact.snapshot != f"content/{artifact.content_hash}.txt" or not re.fullmatch(
                    r"[a-f0-9]{64}", artifact.content_hash
                ):
                    raise ResearchError("Invalid artifact snapshot")
                self._require(
                    artifact.evidence_ids + artifact.assumption_evidence_ids,
                    memory.evidence_pool,
                    "artifact evidence",
                )
                self._require(artifact.finding_ids, memory.conclusions, "artifact finding")
                self._require(artifact.input_artifact_ids, memory.artifacts, "input artifact")
                self._require([artifact.execution_id], memory.executions, "artifact execution")

    @staticmethod
    def _step_evidence(memory: ResearchMemory, ids: list[str]) -> set[str]:
        result: set[str] = set()
        visiting: set[str] = set()
        visited: set[str] = set()

        def walk(key: str) -> None:
            if key in visiting:
                raise ResearchError("Reasoning steps cannot contain a cycle")
            if key in visited:
                return
            step = memory.reasoning_chain[key]
            visiting.add(key)
            result.update(step.evidence_ids)
            for previous in step.prior_step_ids:
                walk(previous)
            visiting.remove(key)
            visited.add(key)

        for key in ids:
            walk(key)
        return result

    @staticmethod
    def _source_identity(source: SourceRecord) -> str:
        parsed = urlsplit(source.locator)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            return source_identity(source.locator)
        if source.locator.startswith("mcp:"):
            return source.locator.split("/", 1)[0]
        locator = source.locator.split("#", 1)[0]
        if source.kind == "file":
            locator = re.sub(r":\d+(?:-\d+)?$", "", locator)
        return f"{source.kind}:{locator}"

    async def _task_has_work(self, memory: ResearchMemory, task_id: str) -> bool:
        recorded = (
            any(
                source.task_id == task_id and not source.is_error
                for source in memory.sources.values()
            )
            or any(
                evidence.task_id == task_id and evidence.status != "retracted"
                for evidence in memory.evidence_pool.values()
            )
            or any(step.task_id == task_id for step in memory.reasoning_chain.values())
        )
        if recorded:
            return True
        from researchx.storage.schema import file_records

        async with self.transaction() as db:
            rows = await db.scalars(
                select(file_records.c.payload).where(
                    file_records.c.workspace_id == self.workspace_id,
                    file_records.c.session_id == self.session_id,
                    file_records.c.kind == "artifacts",
                )
            )
            if any(
                item.get("task_id") == task_id and item.get("status") not in {"failed", "error"}
                for item in rows
            ):
                return True
        return False

    async def capture(
        self,
        *,
        origin_id: str,
        content: str,
        kind: SourceKind = "tool",
        title: str = "",
        locator: str = "",
        fragment: bool = False,
        is_error: bool = False,
        published_at: str | None = None,
        event_at: str | None = None,
        index: int = 0,
        usage: SourceUsage | None = None,
        original_locator: str | None = None,
        corrects_source_id: str | None = None,
        request_profile: Literal["event_commentary", "financial_commentary"] | None = None,
    ) -> SourceRecord:
        """Program-only provenance entrypoint; write immutable content before its reference."""
        source_id = "src_" + hashlib.sha256(f"{origin_id}:{index}".encode()).hexdigest()[:20]
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        async with self.transaction():
            memory = await self._load()
            if source_id in memory.sources:
                if memory.sources[source_id].content_hash != content_hash:
                    raise ResearchError("Source origin ID already used for different content")
                (await self.read_source(memory.sources[source_id]))
                return memory.sources[source_id]
            content_hash, snapshot = await self.write_snapshot(content)
            source = SourceRecord(
                id=source_id,
                plan_id=memory.research_state.current_plan_id,
                task_id=memory.research_state.current_task_id if kind != "user" else None,
                kind=kind,
                title=title or kind,
                locator=locator or f"{kind}:{origin_id}",
                origin_id=origin_id,
                collected_at=now(),
                published_at=published_at,
                event_at=event_at,
                content_hash=content_hash,
                snapshot=snapshot,
                fragment=fragment,
                is_error=is_error,
                usage=usage or SourceUsage(),
                original_locator=original_locator,
                corrects_source_id=corrects_source_id,
                request_profile=request_profile if kind == "user" else None,
            )
            memory.sources[source.id] = source
            (await self._save(memory, "capture_source", {"source_id": source.id}))
            return source

    async def read_source(self, source: SourceRecord) -> str:
        return await self.read_content(source.content_hash)

    async def read_content(self, digest: str) -> str:
        try:
            reference = await self._content_reference(digest)
            return (await self.content.read(reference)).decode("utf-8")
        except (OSError, ValueError) as exc:
            raise ResearchError("资料快照校验失败；原内容已保留") from exc

    async def _read_by_ids(self, operation: ReadMemory, *, model_access: bool) -> dict[str, object]:
        from researchx.state.episodes import EpisodicRepository
        from researchx.state.source_policy import may_enter_external_model

        # read_records takes a shared lock on the revision row. Keep its outer
        # transaction open through permission checks and content retrieval so
        # concurrent source reclassification cannot revoke this snapshot midway.
        async with current_database().transaction():
            revision, records, referenced_sources = await EpisodicRepository(
                self.workspace_id, self.session_id
            ).read_records(operation.ids, strict=model_access)
            if model_access:
                restricted_sources = {
                    key
                    for key, (kind, payload) in records.items()
                    if kind == "source"
                    and (source := SourceRecord.model_validate(payload)).kind != "user"
                    and not may_enter_external_model(source.usage)
                }
                for key in operation.ids:
                    if referenced_sources[key] & restricted_sources:
                        raise ResearchError("来源授权尚未确认，不能向当前模型提供此记录")
            record_results = []
            source_ids: set[str] = set().union(*referenced_sources.values())
            for key in operation.ids:
                kind, item = records[key]
                item = dict(item)
                if kind == "source" and operation.include_content:
                    item["content"] = await self.read_source(SourceRecord.model_validate(item))
                record_results.append(item)
            return {
                "revision": revision,
                "records": record_results,
                "source_ids": sorted(source_ids),
            }

    async def apply(
        self,
        data: dict[str, Any],
        *,
        budget: int | None = None,
        model: str = "",
        model_access: bool = False,
    ) -> dict[str, object]:
        operation = OPERATION_ADAPTER.validate_python(data)
        if isinstance(operation, SearchMemory):
            from researchx.state.episodes import EpisodeQuery, EpisodicRepository

            return await EpisodicRepository(self.workspace_id, self.session_id).search(
                EpisodeQuery(
                    query=operation.query,
                    record_types=tuple(operation.record_types),
                    task_id=operation.task_id,
                    status=operation.status,
                    cursor=operation.cursor,
                    limit=operation.limit,
                )
            )
        if isinstance(operation, ReadMemory):
            if model_access and not operation.ids:
                from researchx.state.episodes import EpisodicRepository

                return await EpisodicRepository(
                    self.workspace_id, self.session_id
                ).context_summary()
            if not operation.ids:
                memory = await self.load()
                return self.model_view(memory) if model_access else self.view(memory)
            return await self._read_by_ids(operation, model_access=model_access)
        fingerprint = hashlib.sha256(
            json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        async with self.transaction():
            memory = await self._load()
            previous = memory.operations.get(operation.operation_id)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ResearchError("operation_id already used with different content")
                return previous["receipt"] | {"current_revision": memory.revision}
            if operation.expected_revision != memory.revision:
                raise ResearchError(
                    f"Revision conflict: expected {operation.expected_revision}, current {memory.revision}; read and retry with a new operation_id"
                )
            result = await self._apply(memory, operation)
            # The old full-aggregate prompt is no longer the request boundary.
            # The bounded context composer checks its own final recall budget.
            progress = await self.progress(memory)
            progress["revision"] = memory.revision + 1
            receipt = {"revision": memory.revision + 1, **result, "progress": progress}
            memory.operations[operation.operation_id] = {
                "fingerprint": fingerprint,
                "receipt": receipt,
            }
            (
                await self._save(
                    memory, operation.action, operation.model_dump(mode="json") | {"result": result}
                )
            )
            return receipt

    @staticmethod
    def _archive_plan(memory: ResearchMemory) -> str | None:
        state = memory.research_state
        old_id = state.current_plan_id
        if old_id:
            plan = memory.plans[old_id]
            plan.archived = True
            for task in plan.tasks:
                if task.status in {"pending", "in_progress", "blocked"}:
                    task.status = "cancelled"
                    task.lease_id = None
                    task.updated_at = now()
        state.current_plan_id = None
        state.current_task_id = None
        state.next_step = ""
        state.unresolved = []
        return old_id

    def _invalidate(self, memory: ResearchMemory, evidence_id: str) -> None:
        affected = {evidence_id}
        while True:
            expanded = affected | {
                item.id
                for item in memory.evidence_pool.values()
                if set(item.input_evidence_ids) & affected
                or set(item.supporting_evidence_ids) & affected
                or (
                    item.verification_step_id
                    and self._step_evidence(memory, [item.verification_step_id]) & affected
                )
                or (
                    item.calculation_step_id
                    and self._step_evidence(memory, [item.calculation_step_id]) & affected
                )
            }
            if expanded == affected:
                break
            affected = expanded
        for key in affected:
            memory.evidence_pool[key].needs_review = True
        for claim in memory.conclusions.values():
            if (set(claim.evidence_ids) | self._step_evidence(memory, claim.step_ids)) & affected:
                claim.needs_review = True
        if memory.project:
            stale = {
                key
                for key, artifact in memory.artifacts.items()
                if (set(artifact.evidence_ids + artifact.assumption_evidence_ids) & affected)
                or any(memory.conclusions[key].needs_review for key in artifact.finding_ids)
            }
            while True:
                expanded = stale | {
                    key
                    for key, artifact in memory.artifacts.items()
                    if set(artifact.input_artifact_ids) & stale
                }
                if expanded == stale:
                    break
                stale = expanded
            for key in stale:
                memory.artifacts[key].stale, memory.artifacts[key].stale_reason = (
                    True,
                    "Evidence version superseded",
                )
            current = memory.plans.get(memory.research_state.current_plan_id or "")
            active_ids = (
                {key for task in current.tasks for key in task.artifact_ids} if current else set()
            )
            if stale & active_ids:
                from researchx.state.repository import ResearchRepository

                memory.research_state.replan_required = True
                memory.project.status = "replanning"
                ResearchRepository.revoke_executions(memory, "Evidence version superseded")
        (self._reopen_affected_conflicts(memory, affected))

    async def _apply(
        self, memory: ResearchMemory, operation: ResearchOperation
    ) -> dict[str, object]:
        if memory.project is not None and isinstance(
            operation, (SetContext, CreatePlan, UpdateTask)
        ):
            raise ResearchError(
                "Report projects use research_project and planner/replanner; legacy plan/task writes cannot bypass Runtime"
            )
        if (
            memory.project is not None
            and not isinstance(operation, ReadMemory)
            and (memory.project.status != "running" or memory.research_state.replan_required)
        ):
            raise ResearchError(
                "Resume and plan the report project before research memory mutations"
            )
        conflict_result = await self._apply_conflict(memory, operation)
        if conflict_result is not None:
            return conflict_result
        state = memory.research_state
        if isinstance(operation, SetContext):
            self._require(operation.user_source_ids, memory.sources, "user source")
            new_context = TaskContext(
                **operation.model_dump(exclude={"action", "operation_id", "expected_revision"}),
                supersedes=memory.current_context_id,
            )
            memory.task_context.append(new_context)
            memory.current_context_id = new_context.id
            if state.current_plan_id:
                self._archive_plan(memory)
                state.replan_required = True
            return {"context_id": new_context.id}
        if isinstance(operation, CreatePlan):
            if not memory.current_context_id:
                raise ResearchError("Set task context before creating a plan")
            self._require(operation.reused_evidence_ids, memory.evidence_pool, "reused evidence")
            replaced = {item.supersedes for item in memory.evidence_pool.values()}
            if any(
                memory.evidence_pool[key].status == "retracted" or key in replaced
                for key in operation.reused_evidence_ids
            ):
                raise ResearchError("Reuse only current, non-retracted evidence versions")
            old_id = self._archive_plan(memory)
            if old_id is None and memory.plans:
                old_id = next(reversed(memory.plans))
            new_plan = ResearchPlan(
                context_id=memory.current_context_id,
                title=operation.title,
                tasks=[ResearchTask(title=title) for title in operation.tasks],
                supersedes=old_id,
                reused_evidence_ids=operation.reused_evidence_ids,
            )
            memory.plans[new_plan.id] = new_plan
            state.current_plan_id = new_plan.id
            state.replan_required = False
            return {
                "plan_id": new_plan.id,
                "tasks": [task.model_dump(mode="json") for task in new_plan.tasks],
            }
        if isinstance(operation, UpdateTask):
            if state.replan_required or not state.current_plan_id:
                raise ResearchError("Create a current plan before updating tasks")
            plan = memory.plans[state.current_plan_id]
            task = next((task for task in plan.tasks if task.id == operation.task_id), None)
            if task is None:
                raise ResearchError("Task does not belong to the current plan")
            transition = (task.status, operation.status)
            allowed = {
                ("pending", "in_progress"),
                ("blocked", "in_progress"),
                ("in_progress", "completed"),
                ("in_progress", "blocked"),
            }
            if transition not in allowed:
                raise ResearchError(f"Invalid task transition: {task.status} -> {operation.status}")
            timestamp = now()
            if operation.status == "in_progress":
                if state.current_task_id and state.current_task_id != task.id:
                    raise ResearchError("Finish or block the current task before starting another")
                task.started_at, task.completed_at = task.started_at or timestamp, None
                task.blocker, task.completion_note = "", ""
                state.current_task_id = task.id
                task.lease_id = new_id("lease")
            elif operation.status == "completed":
                if state.current_task_id != task.id:
                    raise ResearchError("Only the current task can be completed")
                if not operation.completion_note.strip():
                    raise ResearchError("Completing a task requires a completion note")
                if not (await self._task_has_work(memory, task.id)):
                    raise ResearchError(
                        "A task needs recorded source, evidence or reasoning before completion"
                    )
                task.completed_at = timestamp
                task.completion_note = operation.completion_note
                task.blocker = ""
                state.current_task_id = None
                task.lease_id = None
            else:
                if state.current_task_id != task.id:
                    raise ResearchError("Only the current task can be blocked")
                if not operation.blocker.strip():
                    raise ResearchError("Blocking a task requires a blocker")
                task.blocker, task.completed_at = operation.blocker, None
                task.completion_note = ""
                state.current_task_id = None
                task.lease_id = None
            task.status, task.updated_at = operation.status, timestamp
            state.next_step = operation.next_step
            if operation.unresolved is not None:
                state.unresolved = operation.unresolved
            return {"task_id": task.id}
        if isinstance(operation, AddEvidence):
            self._require([operation.source_id], memory.sources, "source")
            source = memory.sources[operation.source_id]
            if memory.project is not None and source.execution_id:
                execution = memory.executions[source.execution_id]
                evidence_plan = memory.plans.get(state.current_plan_id or "")
                task = (
                    next((task for task in evidence_plan.tasks if task.id == source.task_id), None)
                    if evidence_plan
                    else None
                )
                if (
                    execution.status != "committed"
                    or not task
                    or source.task_revision != task.task_revision
                ):
                    raise ResearchError("Stale execution source cannot become current evidence")
            if source.plan_id and source.plan_id != state.current_plan_id:
                evidence_plan = memory.plans.get(state.current_plan_id or "")
                reused_sources = (
                    {
                        memory.evidence_pool[key].source_id
                        for key in evidence_plan.reused_evidence_ids
                    }
                    if evidence_plan
                    else set()
                )
                context = next(
                    (item for item in memory.task_context if item.id == memory.current_context_id),
                    None,
                )
                user_sources = set(context.user_source_ids if context else [])
                if source.id not in reused_sources | user_sources:
                    raise ResearchError(
                        "Explicitly reuse archived evidence in the current plan before registering it again"
                    )
            fields = operation.model_dump(exclude={"action", "operation_id", "expected_revision"})
            evidence = Evidence(
                **fields,
                plan_id=state.current_plan_id,
                task_id=state.current_task_id,
                collected_at=source.collected_at,
                published_at=source.published_at,
            )
            if evidence.supersedes:
                self._require([evidence.supersedes], memory.evidence_pool, "superseded evidence")
                if any(
                    item.supersedes == evidence.supersedes for item in memory.evidence_pool.values()
                ):
                    raise ResearchError("Revise the latest evidence version")
                (self._invalidate(memory, evidence.supersedes))
            memory.evidence_pool[evidence.id] = evidence
            return {"evidence_id": evidence.id}
        if isinstance(operation, AddReasoning):
            if state.replan_required:
                raise ResearchError("Replan before continuing research")
            self._ensure_scope(memory, operation.evidence_ids, operation.prior_step_ids)
            step = ReasoningStep(
                **operation.model_dump(exclude={"action", "operation_id", "expected_revision"}),
                plan_id=state.current_plan_id,
                task_id=state.current_task_id,
            )
            memory.reasoning_chain[step.id] = step
            return {"step_id": step.id}
        if isinstance(operation, VerifyEvidence):
            if state.replan_required:
                raise ResearchError("Replan before verifying evidence")
            self._require([operation.evidence_id], memory.evidence_pool, "evidence")
            self._require(
                [operation.verification_step_id], memory.reasoning_chain, "verification step"
            )
            self._require(
                operation.supporting_evidence_ids, memory.evidence_pool, "supporting evidence"
            )
            original = memory.evidence_pool[operation.evidence_id]
            if original.status == "retracted" or any(
                item.supersedes == original.id for item in memory.evidence_pool.values()
            ):
                raise ResearchError("Verify only the current, non-retracted evidence version")
            fields = original.model_dump(
                exclude={
                    "id",
                    "plan_id",
                    "status",
                    "needs_review",
                    "verification_step_id",
                    "verification_note",
                    "verification_method",
                    "supporting_evidence_ids",
                    "supersedes",
                    "created_at",
                    "task_id",
                }
            )
            checked = Evidence(
                **fields,
                plan_id=state.current_plan_id,
                task_id=state.current_task_id,
                status=operation.level,
                verification_step_id=operation.verification_step_id,
                verification_note=operation.verification_note,
                verification_method=operation.method,
                supporting_evidence_ids=operation.supporting_evidence_ids,
                supersedes=original.id,
            )
            (self._invalidate(memory, original.id))
            memory.evidence_pool[checked.id] = checked
            return {"evidence_id": checked.id, "supersedes": original.id}
        if isinstance(operation, AddConclusion):
            if state.replan_required:
                raise ResearchError("Replan before drawing conclusions")
            self._ensure_scope(memory, operation.evidence_ids, operation.step_ids)
            replaced = {item.supersedes for item in memory.evidence_pool.values()}
            if operation.status != "retracted" and any(
                key in replaced or memory.evidence_pool[key].status == "retracted"
                for key in operation.evidence_ids
            ):
                raise ResearchError("Conclusions must use current, non-retracted evidence")
            claim = Conclusion(
                **operation.model_dump(exclude={"action", "operation_id", "expected_revision"}),
                plan_id=state.current_plan_id,
            )
            for conflict in memory.conflicts.values():
                if (
                    conflict.core
                    and conflict.plan_id == state.current_plan_id
                    and conflict.status != "resolved"
                ):
                    inputs, _, _ = self._conflict_inputs(memory, conflict)
                    if (
                        set(claim.evidence_ids) | self._step_evidence(memory, claim.step_ids)
                    ) & inputs:
                        if claim.status == "verified":
                            raise ResearchError(
                                "Resolve the core conflict before registering a verified conclusion"
                            )
                        claim.needs_review = True
            if any(memory.evidence_pool[key].needs_review for key in claim.evidence_ids):
                if claim.status == "verified":
                    raise ResearchError(
                        "Evidence awaiting review cannot support a verified conclusion"
                    )
                claim.needs_review = True
            if claim.supersedes and any(
                item.supersedes == claim.supersedes for item in memory.conclusions.values()
            ):
                raise ResearchError("Revise the latest conclusion version")
            memory.conclusions[claim.id] = claim
            return {"conclusion_id": claim.id}
        raise ResearchError("Unsupported research operation")

    def _ensure_scope(
        self, memory: ResearchMemory, evidence_ids: list[str], step_ids: list[str]
    ) -> None:
        self._require(evidence_ids, memory.evidence_pool, "evidence")
        self._require(step_ids, memory.reasoning_chain, "reasoning step")
        plan_id = memory.research_state.current_plan_id
        plan = memory.plans.get(plan_id or "")
        allowed = set(plan.reused_evidence_ids if plan else [])
        inputs = set(evidence_ids) | self._step_evidence(memory, step_ids)
        # A prior verification may reference earlier versions of the same explicitly reused source.
        allowed_sources = {memory.evidence_pool[key].source_id for key in allowed}
        if any(
            memory.evidence_pool[key].plan_id != plan_id
            and key not in allowed
            and memory.evidence_pool[key].source_id not in allowed_sources
            for key in inputs
        ):
            raise ResearchError(
                "Explicitly reuse archived evidence in the current plan before analyzing it"
            )

    async def progress(self, memory: ResearchMemory | None = None) -> dict[str, object]:
        memory = memory or (await self.load())
        state = memory.research_state
        plan = memory.plans.get(state.current_plan_id or "")
        tasks = [task.model_dump(mode="json") for task in plan.tasks] if plan else []
        from researchx.state.dispatch_audit import public_dispatch_progress

        dispatches = await public_dispatch_progress(self, memory)
        return {
            "revision": memory.revision,
            "dispatches": dispatches,
            "plan_id": plan.id if plan else None,
            "title": plan.title
            if plan
            else memory.objectives[memory.project.objective_revision].subject
            if memory.project
            else "",
            "tasks": tasks,
            "current_task_id": state.current_task_id,
            "completed": sum(task["status"] == "completed" for task in tasks),
            "total": len(tasks),
            "replan_required": state.replan_required,
            **(
                {
                    "project_status": memory.project.status,
                    "plan_revision": memory.project.plan_revision,
                    "objective_revision": memory.project.objective_revision,
                }
                if memory.project
                else {}
            ),
            "conflicts": [
                {"id": item.id, "question": item.question, "status": item.status, "core": item.core}
                for item in memory.conflicts.values()
                if item.plan_id == state.current_plan_id
            ],
        }

    async def verification_candidates(self, text: str) -> list[dict[str, Any]]:
        """Return cited current evidence that can advance without inventing new sources."""
        memory = await self.load()
        replaced = {item.supersedes for item in memory.evidence_pool.values()}
        checked = [
            item
            for item in memory.evidence_pool.values()
            if item.status in {"source_checked", "verified"}
            and not item.needs_review
            and item.id not in replaced
        ]
        candidates: list[dict[str, Any]] = []
        for match in CITATION_PATTERN.finditer(text):
            key = match.group(1)
            evidence = memory.evidence_pool.get(key)
            if not evidence or key in replaced or evidence.status in {"verified", "retracted"}:
                continue
            source = memory.sources[evidence.source_id]
            methods: list[str] = []
            if (
                evidence.status == "pending"
                and source.kind not in {"search", "user", "calculation"}
                and not source.fragment
                and not source.is_error
            ):
                methods.append("source")
            if (
                evidence.status == "pending"
                and source.kind == "calculation"
                and evidence.input_evidence_ids
                and all(
                    memory.evidence_pool[item].status in {"source_checked", "verified"}
                    and not memory.evidence_pool[item].needs_review
                    for item in evidence.input_evidence_ids
                )
            ):
                methods.append("calculation")
            if evidence.status == "source_checked" and any(
                self._source_identity(memory.sources[item.source_id])
                != self._source_identity(source)
                for item in checked
                if item.id != evidence.id
            ):
                methods.append("cross_source")
            if methods:
                candidates.append(
                    {
                        "evidence_id": evidence.id,
                        "status": evidence.status,
                        "source_id": source.id,
                        "source_kind": source.kind,
                        "methods": methods,
                    }
                )
        return candidates

    @staticmethod
    def view(memory: ResearchMemory) -> dict[str, object]:
        state = memory.research_state
        context = next(
            (item for item in memory.task_context if item.id == memory.current_context_id), None
        )
        plan = memory.plans.get(state.current_plan_id or "")
        superseded_claims = {item.supersedes for item in memory.conclusions.values()}
        superseded_claims.update(
            key for item in memory.arbitrations.values() for key in item.replaced_conclusion_ids
        )
        superseded_evidence = {item.supersedes for item in memory.evidence_pool.values()}
        claims = [
            item
            for item in memory.conclusions.values()
            if item.plan_id == state.current_plan_id
            and item.id not in superseded_claims
            and item.status != "retracted"
        ]
        evidence_ids = set(plan.reused_evidence_ids if plan else [])
        for claim in claims:
            evidence_ids.update(claim.evidence_ids)
        evidence = [
            item
            for item in memory.evidence_pool.values()
            if (item.plan_id == state.current_plan_id or item.id in evidence_ids)
            and item.id not in superseded_evidence
            and item.status != "retracted"
        ]
        steps = [
            item
            for item in memory.reasoning_chain.values()
            if item.plan_id == state.current_plan_id
        ]
        source_ids = {item.source_id for item in evidence}
        recent_sources = [
            item for item in memory.sources.values() if item.plan_id == state.current_plan_id
        ][-8:]
        source_ids.update(item.id for item in recent_sources)
        if context:
            source_ids.update(context.user_source_ids)
        return {
            "session_id": memory.session_id,
            "revision": memory.revision,
            "task_context": context.model_dump(mode="json") if context else None,
            "research_state": state.model_dump(mode="json"),
            "plan": plan.model_dump(mode="json") if plan else None,
            "conclusions": [item.model_dump(mode="json") for item in claims],
            "conflicts": [
                item.model_dump(mode="json")
                for item in memory.conflicts.values()
                if item.plan_id == state.current_plan_id
            ],
            "arbitrations": [
                item.model_dump(mode="json")
                for item in memory.arbitrations.values()
                if memory.conflicts[item.conflict_id].plan_id == state.current_plan_id
            ],
            "evidence_pool": [item.model_dump(mode="json") for item in evidence],
            "reasoning_chain": [item.model_dump(mode="json") for item in steps],
            "sources": [
                item.model_dump(mode="json", exclude={"snapshot"})
                for item in memory.sources.values()
                if item.id in source_ids
            ],
        }

    @staticmethod
    def model_view(memory: ResearchMemory) -> dict[str, object]:
        from researchx.state.source_policy import may_enter_external_model

        view = ResearchStore.view(memory)
        allowed = {
            key for key, source in memory.sources.items() if may_enter_external_model(source.usage)
        }
        view["sources"] = [
            item for item in cast(list[dict[str, Any]], view["sources"]) if item["id"] in allowed
        ]
        view["evidence_pool"] = [
            item
            for item in cast(list[dict[str, Any]], view["evidence_pool"])
            if item["source_id"] in allowed
        ]
        valid_evidence = {item["id"] for item in cast(list[dict[str, Any]], view["evidence_pool"])}
        view["conclusions"] = [
            item
            for item in cast(list[dict[str, Any]], view["conclusions"])
            if set(item["evidence_ids"]) <= valid_evidence
        ]
        view["reasoning_chain"] = [
            item
            for item in cast(list[dict[str, Any]], view["reasoning_chain"])
            if set(item["evidence_ids"]) <= valid_evidence and item["evidence_ids"]
        ]
        # Keep actionable conflict identity, while withholding restricted narratives.
        safe_conflicts = []
        for item in cast(list[dict[str, Any]], view["conflicts"]):
            refs = {key for side in item.get("sides", []) for key in side.get("evidence_ids", [])}
            safe_conflicts.append(
                item
                if refs <= valid_evidence
                else {
                    "id": item["id"],
                    "status": item.get("status"),
                    "core": item.get("core"),
                    "question": "存在待核对冲突；相关来源授权确认前不向模型提供内容。",
                }
            )
        view["conflicts"] = safe_conflicts
        if any(
            source.kind in {"web", "file", "mcp", "search"} and key not in allowed
            for key, source in memory.sources.items()
        ):
            view["arbitrations"] = []
        return view

    async def prompt(self, budget: int = 6000, *, model: str = "") -> str:
        return self._prompt((await self.load()), budget, model=model)

    async def prompt_snapshot(
        self,
        budget: int = 6000,
        *,
        model: str = "",
        memory_budget: int | None = None,
        memory: ResearchMemory | None = None,
    ) -> ContextSnapshot:
        from researchx.services.context.sources import tagged_snapshot

        # A context composer may share its just-loaded view within this one
        # request. No persisted state or snapshot is cached between requests.
        current = memory if memory is not None else await self.load()
        if current.session_id != self.session_id:
            raise ResearchError("Memory snapshot belongs to another session")
        text = self._prompt(current, budget, model=model, memory_budget=memory_budget)
        return tagged_snapshot(text, "research_store")

    def _prompt(
        self,
        memory: ResearchMemory,
        budget: int,
        *,
        model: str = "",
        memory_budget: int | None = None,
    ) -> str:
        view = self.model_view(memory)
        required = {
            key: view[key]
            for key in ("session_id", "revision", "task_context", "research_state", "plan")
        }
        required["omitted"] = "Other records remain available through research_memory(read, ids)."
        if memory.project:
            required["project"] = memory.project.model_dump(
                mode="json", exclude={"delivery_manifest"}
            )
            required["objective"] = memory.objectives[memory.project.objective_revision].model_dump(
                mode="json"
            )
        for bucket in (
            "conflicts",
            "arbitrations",
            "conclusions",
            "evidence_pool",
            "sources",
            "reasoning_chain",
        ):
            required[bucket] = []

        def render(value: dict[str, object]) -> str:
            return (
                "<research_memory>\n"
                + json.dumps(value, ensure_ascii=False)
                + "\n</research_memory>"
            )

        if estimate_tokens(render(required), model) > budget:
            raise ResearchError("研究目标、约束或计划超过注入预算；请精简后继续")
        for bucket in (
            "conflicts",
            "arbitrations",
            "conclusions",
            "evidence_pool",
            "sources",
            "reasoning_chain",
        ):
            for item in reversed(cast(list[object], view[bucket])):
                cast(list[object], required[bucket]).append(item)
                exceeds_memory = False
                if memory_budget is not None:
                    # Whole records only; plan/objective snapshots belong to D.
                    from researchx.services.context.sources import MEMORY_KEYS

                    recalled = sum(
                        estimate_tokens(json.dumps(required[key], ensure_ascii=False), model)
                        for key in MEMORY_KEYS
                        if required[key]
                    )
                    exceeds_memory = recalled > memory_budget
                if estimate_tokens(render(required), model) > budget or exceeds_memory:
                    cast(list[object], required[bucket]).pop()
        return render(required)

    async def interrupt(self, *, request_id: str, target_request_id: str, text: str) -> None:
        """Persist steering before cancellation. Replanning starts only after old work settles."""
        async with self.transaction():
            memory = await self._load()
            existing = memory.pending_steers.get(request_id)
            data: PendingSteer = {
                "target_request_id": target_request_id,
                "text": text,
                "accepted_at": now(),
            }
            if existing:
                if existing["target_request_id"] != target_request_id or existing["text"] != text:
                    raise ResearchError("Steering request ID already used")
                return
            memory.pending_steers[request_id] = data
            if memory.project:
                from researchx.state.repository import ResearchRepository

                (ResearchRepository.feedback_memory(memory, text))
            (await self._save(memory, "accept_steer", {"request_id": request_id, **data}))

    async def require_replan(self, request_id: str) -> None:
        async with self.transaction():
            memory = await self._load()
            if request_id not in memory.pending_steers:
                raise ResearchError("Steering request was not persisted")
            if memory.pending_steers[request_id].get("ready"):
                return
            if memory.project is None:
                self._archive_plan(memory)
                memory.research_state.replan_required = True
            else:
                memory.project.status = "replanning" if memory.project.plan_revision else "planning"
            memory.pending_steers[request_id]["ready"] = True
            (await self._save(memory, "require_replan", {"request_id": request_id}))

    async def recover_pending_steers(self) -> None:
        """Called only when the Web host has no live execution for this session."""
        memory = await self.load()
        for request_id, item in memory.pending_steers.items():
            if not item.get("ready"):
                (await self.require_replan(request_id))

    async def stopped(self) -> None:
        async with self.transaction():
            memory = await self._load()
            if memory.project:
                from researchx.state.repository import ResearchRepository

                if memory.project.status not in {"completed", "failed", "cancelled"}:
                    ResearchRepository.suspend_memory(memory, "执行已停止")
                    (await self._save(memory, "project_stop", {}))
                return
            plan = memory.plans.get(memory.research_state.current_plan_id or "")
            if plan:
                changed = False
                for task in plan.tasks:
                    if task.status == "in_progress":
                        task.status, task.blocker, task.updated_at = "blocked", "执行已停止", now()
                        task.lease_id = None
                        changed = True
                if changed:
                    memory.research_state.current_task_id = None
                    (await self._save(memory, "stop", {}))

    async def invalid_citations(self, text: str) -> list[str]:
        """Validate exact IDs before publishing; never infer an intended source."""
        memory = await self.load()
        invalid = [
            match.group(1)
            for match in CITATION_PATTERN.finditer(text)
            if match.group(1) not in memory.evidence_pool
        ]
        if memory.evidence_pool:
            invalid.extend(match.group(0) for match in re.finditer(r"\[(\d{1,3})\](?!\()", text))
        return list(dict.fromkeys(invalid))

    async def render_answer(self, text: str, answer_id: str) -> tuple[str, dict[str, object]]:
        async with self.transaction():
            memory = await self._load()
            if answer_id in memory.answers:
                answer = memory.answers[answer_id]
                return answer["rendered"], dict(answer)
            cited: dict[str, CitationSnapshot] = {}
            invalid: list[str] = []

            # Display numbers are local to one frozen answer. The model must
            # supply stable evidence IDs; bare numbers have no proven target.
            if memory.evidence_pool:

                def reject_number(match: re.Match[str]) -> str:
                    invalid.append(match.group(0))
                    return "[来源不可核验]"

                numbered = re.compile(r"\[(\d{1,3})\](?!\()")
                text = numbered.sub(reject_number, text)

            def replace(match: re.Match[str]) -> str:
                key = match.group(1)
                if key not in memory.evidence_pool:
                    invalid.append(key)
                    return "[来源不可核验]"
                if key not in cited:
                    evidence = memory.evidence_pool[key]
                    source = memory.sources[evidence.source_id]
                    cited[key] = {
                        "number": len(cited) + 1,
                        "evidence": evidence.model_dump(mode="json"),
                        "source": cast(
                            SourceDisplay, source.model_dump(mode="json", exclude={"snapshot"})
                        ),
                    }
                return f"[{cited[key]['number']}]"

            rendered = CITATION_PATTERN.sub(replace, text)
            if cited:
                lines = ["", "", "来源："]
                for item in cited.values():
                    evidence, source = item["evidence"], item["source"]
                    title = re.sub(r"[\[\]\n\r<>]", " ", source["title"]).strip()[:160]
                    locator = source["locator"]
                    parsed = urlsplit(locator)
                    if parsed.scheme in {"http", "https"} and parsed.netloc:
                        reference = f"[{title}]({locator.replace(')', '%29').replace('(', '%28')})"
                    else:
                        clean_locator = re.sub(r"[\n\r<>]", " ", locator)
                        reference = f"{title}（{clean_locator}）"
                    date = (
                        source["published_at"]
                        or f"采集 {source['collected_at'][:10]}；发布日期未知"
                    )
                    if evidence["status"] == "retracted":
                        mark = "已撤回"
                    elif evidence["needs_review"]:
                        mark = "待复核"
                    elif evidence["status"] == "verified":
                        mark = "已核验"
                    elif evidence["status"] == "source_checked":
                        mark = "已核对原文"
                    elif source["kind"] == "search":
                        mark = "待核验（搜索摘要，需读取原文）"
                    elif source["kind"] == "user":
                        mark = "待核验（用户提供信息，缺少可核对原文）"
                    elif source["fragment"]:
                        mark = "待核验（仅有片段，需补充完整原文）"
                    else:
                        mark = "待核验（尚未完成原文核对）"
                    replacement = next(
                        (
                            item
                            for item in memory.evidence_pool.values()
                            if item.supersedes == evidence["id"]
                        ),
                        None,
                    )
                    if replacement is not None:
                        mark = "已撤回" if replacement.status == "retracted" else "已更正，待复核"
                    lines.append(f"[{item['number']}] {reference} · {date} · {mark}")
                rendered += "\n".join(lines)
            answer = {
                "answer_id": answer_id,
                "rendered": rendered,
                "model_text": text,
                "memory_revision": memory.revision,
                "citations": cited,
                "invalid": invalid,
                "created_at": now(),
            }
            memory.answers[answer_id] = answer
            (
                await self._save(
                    memory, "answer", {"answer_id": answer_id, "evidence_ids": (list(cited))}
                )
            )
            return rendered, dict(answer)

    async def completion_warning(self) -> str | None:
        memory = await self.load()
        warnings = []
        if memory.project and memory.project.status != "completed":
            warnings.append("研报项目尚未通过完成验证，当前内容为阶段性结果。")
        if memory.research_state.replan_required:
            warnings.append("研究计划尚未重新生成，研究未完成。")
        plan = memory.plans.get(memory.research_state.current_plan_id or "")
        if plan and any(task.status != "completed" for task in plan.tasks):
            warnings.append("研究任务尚未全部完成，当前回复仅代表阶段性结果。")
        pending = [
            item.question
            for item in memory.conflicts.values()
            if item.plan_id == memory.research_state.current_plan_id
            and item.core
            and item.status != "resolved"
        ]
        if pending:
            warnings.append(
                "以下核心争议尚未解决，相关判断不能作为确定事实：" + "；".join(pending) + "。"
            )
        return "\n".join(warnings) or None
