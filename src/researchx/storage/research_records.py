"""Relational persistence for the existing ResearchMemory domain view."""

from __future__ import annotations

import time
from functools import lru_cache
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

from sqlalchemy import Select, bindparam, and_, delete, select, update, literal, union_all, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import BindParameter

from researchx.state.errors import ResearchError
from researchx.state.models import ResearchMemory
from researchx.storage import schema as s
from researchx.storage.content import ContentReference, ContentStore, persist_object, read_object
from researchx.storage.database import Database


def scope(table: Any, workspace: str, session: str) -> Any:
    return and_(table.c.workspace_id == workspace, table.c.session_id == session)


async def ensure_session(db: AsyncSession, workspace: str, session: str, cwd: str) -> None:
    # Existing sessions are the hot path. Avoid INSERT ON CONFLICT for every read:
    # it adds unnecessary database traffic and row/key locking during SSE updates.
    existing = (
        await db.execute(
            select(s.workspaces.c.canonical_path, s.research_sessions.c.session_id)
            .select_from(
                s.workspaces.outerjoin(
                    s.research_sessions, scope(s.research_sessions, workspace, session)
                )
            )
            .where(s.workspaces.c.workspace_id == workspace)
        )
    ).one_or_none()
    if existing is not None:
        if existing.canonical_path != cwd:
            raise ResearchError("Workspace identity conflict")
        if existing.session_id is not None:
            return
    else:
        await db.execute(
            insert(s.workspaces)
            .values(workspace_id=workspace, canonical_path=cwd, updated_at=time.time())
            .on_conflict_do_nothing()
        )
        path = await db.scalar(
            select(s.workspaces.c.canonical_path).where(s.workspaces.c.workspace_id == workspace)
        )
        if path != cwd:
            raise ResearchError("Workspace identity conflict")
    await db.execute(
        insert(s.research_sessions)
        .values(workspace_id=workspace, session_id=session, updated_at=time.time())
        .on_conflict_do_nothing()
    )


@lru_cache(maxsize=1)
def _snapshot_query() -> Select[Any]:
    # Cache only the immutable SQL expression, never state or query results.
    workspace: BindParameter[str] = bindparam("view_workspace")
    session: BindParameter[str] = bindparam("view_session")

    def view_scope(table: Any) -> Any:
        return and_(table.c.workspace_id == workspace, table.c.session_id == session)

    header = (
        select(
            literal("__header").label("field"),
            literal("").label("record_id"),
            literal(0).label("position"),
            func.jsonb_build_object(
                "session_id",
                s.research_sessions.c.session_id,
                "revision",
                s.research_sessions.c.revision,
                "schema_version",
                s.research_sessions.c.schema_version,
                "current_context_id",
                s.research_sessions.c.current_context_id,
                "research_state",
                s.research_sessions.c.research_state,
                "canonical_path",
                s.workspaces.c.canonical_path,
            ).label("payload"),
        )
        .select_from(
            s.research_sessions.join(
                s.workspaces, s.workspaces.c.workspace_id == s.research_sessions.c.workspace_id
            )
        )
        .where(view_scope(s.research_sessions))
    )
    queries = [header]
    queries.extend(
        select(literal(field), table.c.record_id, table.c.position, table.c.payload).where(
            view_scope(table)
        )
        for field, table in s.entities.items()
    )
    queries.extend(
        [
            select(
                literal("__tasks"),
                s.research_tasks.c.plan_id,
                s.research_tasks.c.position,
                s.research_tasks.c.payload,
            ).where(view_scope(s.research_tasks)),
            select(
                literal("__history"),
                literal(""),
                s.research_history.c.sequence,
                s.research_history.c.payload,
            ).where(view_scope(s.research_history)),
            select(
                literal("__receipts"),
                s.research_receipts.c.operation_id,
                literal(0),
                func.jsonb_build_object(
                    "fingerprint",
                    s.research_receipts.c.fingerprint,
                    "receipt",
                    s.research_receipts.c.receipt,
                ),
            ).where(view_scope(s.research_receipts)),
        ]
    )
    query = union_all(*queries).subquery()
    return select(query).order_by(query.c.field, query.c.position)


class ResearchRecords:
    def __init__(self, database: Database, workspace: str, session: str, cwd: str) -> None:
        self.database, self.workspace, self.session, self.cwd = database, workspace, session, cwd

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        async with self.database.transaction() as db:
            key = ("research_lock", self.workspace, self.session)
            if key not in db.info:
                await ensure_session(db, self.workspace, self.session, self.cwd)
                await db.execute(
                    select(s.research_sessions.c.revision)
                    .where(scope(s.research_sessions, self.workspace, self.session))
                    .with_for_update()
                )
                db.info[key] = True
            yield db

    async def load(self, db: AsyncSession) -> ResearchMemory:
        # A single SQL statement gives a consistent MVCC snapshot under READ COMMITTED.
        # Read-only runtime/context refreshes need neither a write lock nor a WAL commit.
        rows = (
            (
                await db.execute(
                    _snapshot_query(),
                    {"view_workspace": self.workspace, "view_session": self.session},
                )
            )
            .mappings()
            .all()
        )
        headers = [row["payload"] for row in rows if row["field"] == "__header"]
        if not headers:
            await ensure_session(db, self.workspace, self.session, self.cwd)
            return await self.load(db)
        data = dict(headers[0])
        if data.pop("canonical_path") != self.cwd:
            raise ResearchError("Workspace identity conflict")
        for field in s.entities:
            data[field] = [] if field == "task_context" else None if field == "project" else {}
        data["history"], data["operations"] = [], {}
        for row in rows:
            field, payload = row["field"], row["payload"]
            if field == "__header" or field == "__tasks":
                continue
            if field == "__history":
                details = payload.get("data", {})
                body = details.get("content") if isinstance(details, dict) else None
                if (
                    payload.get("action") == "submit_artifact"
                    and isinstance(body, dict)
                    and set(body) == {"content_ref"}
                ):
                    if not isinstance(body["content_ref"], str):
                        raise ResearchError("Invalid history content reference")
                    payload = {
                        **payload,
                        "data": {
                            **details,
                            "content": (
                                await read_object(
                                    self.database, self.workspace, body["content_ref"]
                                )
                            ).decode("utf-8"),
                        },
                    }
                data["history"].append(payload)
            elif field == "__receipts":
                data["operations"][row["record_id"]] = payload
            elif field == "task_context":
                data[field].append(payload)
            elif field == "project":
                data[field] = payload
            else:
                data[field][row["record_id"]] = payload
        for row in rows:
            if row["field"] == "__tasks":
                data["plans"][row["record_id"]].setdefault("tasks", []).append(row["payload"])
        memory = ResearchMemory.model_validate(data)
        # Transaction-local comparison only, discarded with AsyncSession. PostgreSQL remains authority.
        db.info[("research_snapshot", self.workspace, self.session)] = memory.model_dump(
            mode="json"
        )
        return memory

    async def register_content(
        self, db: AsyncSession, content: ContentStore, references: list[ContentReference]
    ) -> None:
        for reference in references:
            if reference.workspace_id != self.workspace:
                raise ResearchError("Content reference belongs to another workspace")
            await content.verify(reference)
            await db.execute(
                insert(s.content_objects).values(**asdict(reference)).on_conflict_do_nothing()
            )
            row = (
                (
                    await db.execute(
                        select(s.content_objects).where(
                            s.content_objects.c.workspace_id == self.workspace,
                            s.content_objects.c.content_hash == reference.content_hash,
                        )
                    )
                )
                .mappings()
                .one()
            )
            if row["object_key"] != reference.object_key or row["size"] != reference.size:
                raise ResearchError("Existing content reference conflicts with object")

    async def save(
        self,
        db: AsyncSession,
        memory: ResearchMemory,
        validate: Callable[[ResearchMemory], None],
        *,
        expected_revision: int,
    ) -> None:
        if memory.session_id != self.session:
            raise ResearchError("Research session mismatch")
        validate(memory)
        revision = await db.scalar(
            select(s.research_sessions.c.revision)
            .where(scope(s.research_sessions, self.workspace, self.session))
            .with_for_update()
        )
        if revision != expected_revision:
            raise ResearchError(
                f"Revision conflict: expected {expected_revision}, current {revision}"
            )
        if memory.revision != expected_revision + 1:
            raise ResearchError("Research revision must advance exactly once")
        data = memory.model_dump(mode="json")
        previous = db.info.get(("research_snapshot", self.workspace, self.session))
        if previous is None or previous["revision"] != expected_revision:
            previous = (await self.load(db)).model_dump(mode="json")
        # Delete only removed identities; all changed rows and the header share this transaction.
        task_rows = []
        plans_changed = data["plans"] != previous["plans"]
        for field, table in s.entities.items():
            if data[field] == previous[field]:
                continue
            values = data[field]
            old_values = previous[field]
            if field == "task_context":
                values = {item["id"]: item for item in values}
                old_values = {item["id"]: item for item in old_values}
            elif field == "project":
                values = {values["id"]: values} if values else {}
                old_values = {old_values["id"]: old_values} if old_values else {}
            removed = set(old_values) - set(values)
            if removed:
                await db.execute(
                    delete(table).where(
                        scope(table, self.workspace, self.session), table.c.record_id.in_(removed)
                    )
                )
            old_positions = {identity: position for position, identity in enumerate(old_values)}
            for position, (identity, raw) in enumerate(values.items()):
                payload = dict(raw)
                if field == "plans":
                    for order, task in enumerate(payload.pop("tasks")):
                        task_rows.append(
                            {
                                "workspace_id": self.workspace,
                                "session_id": self.session,
                                "plan_id": identity,
                                "record_id": task["id"],
                                "position": order,
                                "status": task["status"],
                                "revision": task.get("task_revision", 1),
                                "payload": task,
                            }
                        )
                if raw == old_values.get(identity) and position == old_positions.get(identity):
                    continue
                row = {
                    "workspace_id": self.workspace,
                    "session_id": self.session,
                    "record_id": str(identity),
                    "position": position,
                    "payload": payload,
                    "status": payload.get("status"),
                    "revision": payload.get("revision"),
                    "project_id": payload.get("project_id"),
                    "plan_id": payload.get("plan_id"),
                    "task_id": payload.get("task_id"),
                    "content_hash": payload.get("content_hash"),
                }
                for column in ("context_id", "source_id", "execution_id"):
                    if column in table.c:
                        row[column] = payload.get(column)
                stmt = insert(table).values(**row)
                await db.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["workspace_id", "session_id", "record_id"],
                        set_={
                            key: value
                            for key, value in row.items()
                            if key not in {"workspace_id", "session_id", "record_id"}
                        },
                    )
                )
        if plans_changed:
            await db.execute(
                delete(s.research_tasks).where(
                    scope(s.research_tasks, self.workspace, self.session)
                )
            )
            if task_rows:
                await db.execute(insert(s.research_tasks), task_rows)
        existing_history = previous["history"]
        if data["history"][: len(existing_history)] != existing_history:
            raise ResearchError("Research history is append-only")
        for sequence, event in enumerate(
            data["history"][len(existing_history) :], start=len(existing_history)
        ):
            stored_event = event
            details = event.get("data", {})
            if (
                event.get("action") == "submit_artifact"
                and isinstance(details, dict)
                and isinstance(details.get("content"), str)
            ):
                # Keep the domain view/serialization intact without duplicating report
                # bodies inside an audit JSONB row. The object is verified before SQL commit.
                reference = await persist_object(
                    self.database, self.workspace, details["content"].encode()
                )
                stored_event = {**event, "data": {**details, "content": {"content_ref": reference}}}
            stmt = insert(s.research_history).values(
                workspace_id=self.workspace,
                session_id=self.session,
                sequence=sequence,
                revision=event["revision"],
                action=event["action"],
                payload=stored_event,
            )
            await db.execute(stmt.on_conflict_do_nothing())
        if not set(previous["operations"]).issubset(data["operations"]):
            raise ResearchError("Operation receipts are immutable")
        for operation, item in data["operations"].items():
            if operation in previous["operations"]:
                if item != previous["operations"][operation]:
                    raise ResearchError("operation_id already used with different content")
                continue
            existing = (
                (
                    await db.execute(
                        select(s.research_receipts).where(
                            scope(s.research_receipts, self.workspace, self.session),
                            s.research_receipts.c.operation_id == operation,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing:
                if (
                    existing["fingerprint"] != item["fingerprint"]
                    or existing["receipt"] != item["receipt"]
                ):
                    raise ResearchError("operation_id already used with different content")
                continue
            await db.execute(
                insert(s.research_receipts).values(
                    workspace_id=self.workspace,
                    session_id=self.session,
                    operation_id=operation,
                    fingerprint=item["fingerprint"],
                    receipt=item["receipt"],
                    revision=item["receipt"].get("revision", memory.revision),
                )
            )
        # A changed evidence/source/artifact invalidates its frozen human approval
        # in the same transaction as the authoritative research revision.
        frozen = (
            (
                await db.execute(
                    select(s.deliverables).where(
                        scope(s.deliverables, self.workspace, self.session),
                        s.deliverables.c.status.in_(["awaiting_review", "approved"]),
                    )
                )
            )
            .mappings()
            .all()
        )
        from researchx.state.deliveries import dependencies_current

        for delivery in frozen:
            if not dependencies_current(memory, delivery["manifest"]):
                await db.execute(
                    update(s.deliverables)
                    .where(
                        scope(s.deliverables, self.workspace, self.session),
                        s.deliverables.c.delivery_id == delivery["delivery_id"],
                    )
                    .values(status="invalidated", updated_at=time.time())
                )
        await db.execute(
            update(s.research_sessions)
            .where(scope(s.research_sessions, self.workspace, self.session))
            .values(
                revision=memory.revision,
                schema_version=memory.schema_version,
                current_context_id=memory.current_context_id,
                research_state=data["research_state"],
                updated_at=time.time(),
            )
        )
        db.info[("research_snapshot", self.workspace, self.session)] = data
