"""Durable candidate dispatch audit and nonblocking process-lifetime ownership."""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TYPE_CHECKING
from collections.abc import Mapping
import socket
import os
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from researchx.storage.schema import dispatches
from researchx.storage.research_records import scope

if TYPE_CHECKING:
    from researchx.state.store import ResearchStore
    from researchx.state.models import ResearchMemory
from typing_extensions import TypedDict, NotRequired
from pydantic import TypeAdapter
from researchx.storage.filesystem import private_directory
from researchx.state.errors import ResearchError


class AssignmentRecord(TypedDict):
    task_id: str
    instruction: str
    title: NotRequired[str | None]
    context: str | None


class DispatchAudit(TypedDict, total=False):
    dispatch_id: str
    project_id: str | None
    parent_task_id: str
    parent_execution_id: str | None
    task_revision: int
    runtime_id: str | None
    schema_version: int
    retry_of: str | None
    baseline: list[int]
    status: str
    tasks: list[AssignmentRecord]
    results: list[dict[str, object]]
    recovered: bool
    version: int
    scope: dict[str, object]
    branches: list[dict[str, object]]


def dispatch_directory(store_directory: Path, dispatch_id: str) -> Path:
    if not re.fullmatch(r"dispatch_[a-f0-9]{32}", dispatch_id):
        raise ResearchError("Invalid dispatch ID")
    directory = store_directory / "dispatches" / dispatch_id
    if directory.is_symlink() or directory.resolve() != directory.absolute():
        raise ResearchError("Invalid dispatch audit directory")
    return directory


@contextmanager
def lifecycle_lock(directory: Path) -> Iterator[bool]:
    """False means an owner is alive. The kernel releases this lock on SIGKILL."""
    private_directory(directory)
    with (directory / ".lifecycle.lock").open("a+b") as stream:
        acquired = False
        if __import__("os").name == "nt":
            import msvcrt

            (stream.write(b"\0"))
            stream.flush()
            stream.seek(0)
            try:
                getattr(msvcrt, "locking")(stream.fileno(), getattr(msvcrt, "LK_NBLCK"), 1)
                acquired = True
            except OSError:
                pass
        else:
            import fcntl

            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
        try:
            yield acquired
        finally:
            if acquired:
                if __import__("os").name == "nt":
                    stream.seek(0)
                    getattr(msvcrt, "locking")(stream.fileno(), getattr(msvcrt, "LK_UNLCK"), 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


async def read_batch(store: ResearchStore, dispatch_id: str) -> DispatchAudit:
    dispatch_directory(store.directory, dispatch_id)
    async with store.transaction() as db:
        payload = await db.scalar(
            select(dispatches.c.payload).where(
                scope(dispatches, store.workspace_id, store.session_id),
                dispatches.c.dispatch_id == dispatch_id,
            )
        )
    if payload is None:
        raise ResearchError("Unknown dispatch in this session/workspace")
    batch = TypeAdapter(DispatchAudit).validate_python(payload, strict=True)
    if batch.get("schema_version", 1) not in {1, 2} or batch.get("dispatch_id") != dispatch_id:
        raise ResearchError("Invalid dispatch identity/schema")
    return batch


async def write_batch(store: ResearchStore, payload: Mapping[str, object]) -> None:
    batch = TypeAdapter(DispatchAudit).validate_json(json.dumps(payload), strict=True)
    identifier = batch["dispatch_id"]
    dispatch_directory(store.directory, identifier)
    owner = f"{socket.gethostname()}:{os.getpid()}"
    async with store.transaction() as db:
        existing = (
            (
                await db.execute(
                    select(dispatches)
                    .where(
                        scope(dispatches, store.workspace_id, store.session_id),
                        dispatches.c.dispatch_id == identifier,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing and existing["owner"] != owner:
            raise ResearchError("Dispatch belongs to another owner")
        old = existing["payload"] if existing else {}
        batch["version"] = int(old.get("version", 0)) + 1
        if "branches" not in batch:
            batch["branches"] = old.get(
                "branches",
                [
                    {
                        "task_id": task["task_id"],
                        "title": task.get("title") or task["instruction"][:120],
                        "status": "queued",
                    }
                    for task in batch.get("tasks", [])
                ],
            )
        outcomes = {item["task_id"]: item for item in batch.get("results", [])}
        for branch in batch.get("branches", []):
            result = outcomes.get(str(branch["task_id"]))
            if result:
                branch["status"] = (
                    "awaiting_review" if result["status"] == "completed" else result["status"]
                )
        values = dict(
            workspace_id=store.workspace_id,
            session_id=store.session_id,
            dispatch_id=identifier,
            status=batch["status"],
            owner=owner,
            payload=batch,
        )
        statement = insert(dispatches).values(**values)
        await db.execute(
            statement.on_conflict_do_update(
                index_elements=["workspace_id", "session_id", "dispatch_id"],
                set_={"status": batch["status"], "payload": batch},
            )
        )


async def write_branch(store: ResearchStore, dispatch_id: str, task_id: str, status: str) -> None:
    from researchx.state.models import now

    async with store.transaction():
        batch = await read_batch(store, dispatch_id)
        branch = next(item for item in batch.get("branches", []) if item["task_id"] == task_id)
        branch.update(status=status, updated_at=now())
        if status == "running":
            branch["started_at"] = now()
        await write_batch(store, batch)


async def authorised_candidate_file(store: ResearchStore, path: Path, digest: str) -> bool:
    """Only unchanged host-recorded child notes may re-enter the parent model."""
    from researchx.state.models import SourceUsage
    from researchx.state.source_policy import may_enter_external_model

    memory = await store.load()
    async with store.transaction() as db:
        payloads = await db.scalars(
            select(dispatches.c.payload).where(
                scope(dispatches, store.workspace_id, store.session_id)
            )
        )
        for batch in payloads:
            for result in batch.get("results", []):
                if result.get("status") != "completed":
                    continue
                candidates = {
                    item["content_hash"]: item for item in result.get("candidate_sources", [])
                }
                for record in result.get("output_records", []):
                    if record.get("path") != str(path) or record.get("hash") != digest:
                        continue
                    for key in record.get("source_hashes", []):
                        source = candidates.get(key)
                        if source is None or not may_enter_external_model(
                            SourceUsage.model_validate(source.get("__host_usage", {}))
                        ):
                            return False
                        if any(
                            item.content_hash == key and not may_enter_external_model(item.usage)
                            for item in memory.sources.values()
                        ):
                            return False
                    for key in record.get("evidence_refs", []):
                        evidence = memory.evidence_pool.get(key)
                        if evidence is None or not may_enter_external_model(
                            memory.sources[evidence.source_id].usage
                        ):
                            return False
                    return True
    return False


async def public_dispatch_progress(
    store: ResearchStore, memory: ResearchMemory
) -> list[dict[str, object]]:
    """Only public branch labels/status; never expose instructions or source bodies."""
    async with store.records.database.transaction() as db:
        payloads = await db.scalars(
            select(dispatches.c.payload).where(
                scope(dispatches, store.workspace_id, store.session_id)
            )
        )
        output = []
        plan = memory.plans.get(memory.research_state.current_plan_id or "")
        for batch in payloads:
            binding = batch.get("scope", {})
            if binding:
                if binding.get("plan_id") != memory.research_state.current_plan_id:
                    continue
            elif not memory.project or batch.get("project_id") != memory.project.id:
                continue
            results = {item["task_id"]: item for item in batch.get("results", [])}
            branches = []
            for branch in batch.get("branches", []):
                item = dict(branch)
                result = results.get(str(item["task_id"]), {})
                candidates = [
                    candidate
                    for candidate in result.get("candidate_sources", [])
                    if not candidate.get("fragment", True) and candidate.get("kind") != "search"
                ]
                hashes = {
                    candidate["content_ref"].removeprefix("sha256:") for candidate in candidates
                }
                replaced = {
                    evidence.supersedes
                    for evidence in memory.evidence_pool.values()
                    if evidence.supersedes
                }
                checked_records = {
                    evidence.id: evidence
                    for evidence in memory.evidence_pool.values()
                    if evidence.id not in replaced
                    and evidence.status in {"source_checked", "verified"}
                    and not evidence.needs_review
                    and (
                        evidence.plan_id == memory.research_state.current_plan_id
                        or bool(plan and evidence.id in plan.reused_evidence_ids)
                    )
                }
                checked = {
                    memory.sources[evidence.source_id].content_hash
                    for evidence in checked_records.values()
                }
                parent = (
                    next(
                        (task for task in plan.tasks if task.id == batch.get("parent_task_id")),
                        None,
                    )
                    if plan
                    else None
                )
                refs = set(result.get("evidence_refs", []))
                used = set(parent.finding_ids) if parent else set()
                if parent:
                    used.update(
                        evidence.id
                        for evidence in checked_records.values()
                        if evidence.task_id == parent.id
                    )
                    for artifact_id in parent.artifact_ids:
                        artifact = memory.artifacts.get(artifact_id)
                        if artifact:
                            used.update(artifact.evidence_ids)
                    for criterion_refs in parent.criterion_results.values():
                        used.update(criterion_refs)
                checked_inputs = bool(hashes and hashes <= checked) or bool(
                    refs and refs <= checked_records.keys() and refs <= used
                )
                item["reviewed"] = bool(
                    result.get("status") == "completed"
                    and checked_inputs
                    and parent
                    and parent.status == "completed"
                    and parent.task_revision == batch.get("task_revision", parent.task_revision)
                )
                branches.append(item)
            output.append(
                {
                    "dispatch_id": batch["dispatch_id"],
                    "version": batch.get("version", 0),
                    "parent_task_id": batch.get("parent_task_id"),
                    "status": batch["status"],
                    "branches": branches,
                }
            )
        return output


async def write_result(store: ResearchStore, dispatch_id: str, result: dict[str, object]) -> None:
    async with store.transaction():
        batch = await read_batch(store, dispatch_id)
        if result["task_id"] not in {task["task_id"] for task in batch["tasks"]}:
            raise ResearchError("Unknown dispatch assignment")
        results = batch.setdefault("results", [])
        previous = next((item for item in results if item["task_id"] == result["task_id"]), None)
        if previous is not None:
            if previous != result:
                raise ResearchError("Dispatch result already committed with different content")
            return
        results.append(result)
        for branch in batch.get("branches", []):
            if branch["task_id"] == result["task_id"]:
                from researchx.state.models import now

                branch.update(
                    status="awaiting_review"
                    if result["status"] == "completed"
                    else result["status"],
                    completed_at=now(),
                    updated_at=now(),
                )
        await write_batch(store, dict(batch))


async def recover_dispatches(
    store: ResearchStore, project_id: str | None, workspace: Path | None = None
) -> bool:
    async with store.transaction() as db:
        rows = (
            (
                await db.execute(
                    select(dispatches).where(
                        scope(dispatches, store.workspace_id, store.session_id),
                        dispatches.c.status == "running",
                    )
                )
            )
            .mappings()
            .all()
        )
        active = False
        for row in rows:
            batch = TypeAdapter(DispatchAudit).validate_python(row["payload"], strict=True)
            if batch.get("schema_version", 1) not in {1, 2}:
                raise ResearchError("Unsupported dispatch audit schema")
            if batch.get("project_id") != project_id:
                continue
            if not row["owner"] or not row["owner"].startswith(socket.gethostname() + ":"):
                active = True
                continue
            directory = dispatch_directory(store.directory, batch["dispatch_id"])
            with lifecycle_lock(directory) as acquired:
                if not acquired:
                    active = True
                    continue
                results = {item["task_id"]: item for item in batch.get("results", [])}
                for task in batch.get("tasks", []):
                    task_id = task["task_id"]
                    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", task_id):
                        raise ResearchError("Invalid persisted subagent task ID")
                    if task_id in results:
                        continue
                    paths = []
                    if workspace:
                        from researchx.state.runtime import ResearchAgentRuntime

                        output = workspace / "subagents" / directory.name / task_id
                        for path in sorted(output.rglob("*")):
                            try:
                                checked = ResearchAgentRuntime._check_path(workspace, path)
                                if checked.is_file():
                                    paths.append(str(checked.relative_to(workspace)))
                            except (ResearchError, OSError):
                                continue
                            if len(paths) >= 50:
                                break
                    results[task_id] = {
                        "task_id": task_id,
                        "status": "interrupted",
                        "summary": "",
                        "evidence_refs": [],
                        "output_paths": paths,
                        "errors": ["Dispatch owner exited before durable completion"],
                    }
                for branch in batch.get("branches", []):
                    if branch.get("status") in {"queued", "running"}:
                        branch["status"] = "interrupted"
                batch["version"] = batch.get("version", 0) + 1
                batch.update(
                    {"status": "interrupted", "results": list(results.values()), "recovered": True}
                )
                await db.execute(
                    update(dispatches)
                    .where(
                        scope(dispatches, store.workspace_id, store.session_id),
                        dispatches.c.dispatch_id == batch["dispatch_id"],
                    )
                    .values(status="interrupted", payload=batch)
                )
        return active


async def dispatch_summaries(store: ResearchStore, project_id: str) -> list[dict[str, object]]:
    async with store.transaction() as db:
        batches = await db.scalars(
            select(dispatches.c.payload)
            .where(
                scope(dispatches, store.workspace_id, store.session_id),
            )
            .order_by(dispatches.c.dispatch_id)
        )
        return [
            {
                key: batch.get(key)
                for key in (
                    "dispatch_id",
                    "schema_version",
                    "parent_execution_id",
                    "parent_task_id",
                    "task_revision",
                    "baseline",
                    "status",
                    "retry_of",
                    "results",
                )
            }
            | {"candidate_authority": "unreviewed", "requires_main_review": True}
            for batch in batches
            if batch.get("project_id") == project_id
        ]
