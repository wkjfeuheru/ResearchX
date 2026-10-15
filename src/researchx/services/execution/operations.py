"""PostgreSQL execution ledger, with workspace locks around resource claims."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from researchx.storage import schema as s
from researchx.storage.database import Database, current_database, workspace_id
from pathlib import Path
from researchx.config.paths import get_data_dir


STATES = {
    "prepared",
    "running",
    "succeeded",
    "failed",
    "partial",
    "uncertain",
    "cancelled",
    "blocked",
}
EDGES = {
    "prepared": {"running", "blocked", "cancelled"},
    "running": {"succeeded", "failed", "partial", "uncertain", "cancelled", "blocked"},
    "failed": set(),
    "blocked": set(),
    "cancelled": set(),
    "succeeded": set(),
    "uncertain": {"succeeded", "failed"},
    "partial": {"succeeded", "failed"},
}


class OperationStore:
    def __init__(
        self, cwd: str | Path, *, database: Database | None = None, directory: Path | None = None
    ) -> None:
        from pathlib import Path

        self.database = database or current_database()
        self.directory = directory or get_data_dir() / "executions"
        self.cwd = str(Path(cwd).resolve())
        self.workspace = workspace_id(self.cwd)
        # A PID only has meaning on the originating host; never probe a remote PID locally.
        self.host = socket.gethostname()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        async with self.database.transaction() as db:
            lock = (
                select(s.workspaces.c.canonical_path)
                .where(s.workspaces.c.workspace_id == self.workspace)
                .with_for_update()
            )
            path = await db.scalar(lock)
            if path is None:
                await db.execute(
                    insert(s.workspaces)
                    .values(
                        workspace_id=self.workspace, canonical_path=self.cwd, updated_at=time.time()
                    )
                    .on_conflict_do_nothing()
                )
                path = await db.scalar(lock)
            if path != self.cwd:
                raise ValueError("Workspace identity conflict")
            yield db

    def _where(self, operation: str) -> Any:
        return (s.tool_operations.c.workspace_id == self.workspace) & (
            s.tool_operations.c.operation_id == operation
        )

    async def _row(self, db: AsyncSession, operation: str, *, lock: bool = True) -> dict[str, Any]:
        query = select(s.tool_operations).where(self._where(operation))
        if lock:
            query = query.with_for_update()
        row = (await db.execute(query)).mappings().one_or_none()
        if row is None:
            raise ValueError("Unknown operation in this workspace")
        return dict(row)

    async def prepare(
        self,
        *,
        session: str,
        scope: str,
        run: str,
        call: str,
        tool: str,
        version: str,
        digest: str,
        effect: str,
        resources: dict[str, list[str]],
    ) -> dict[str, Any]:
        operation = operation_identity(self.workspace, session, scope, call)
        run = f"{self.workspace}:{run}"
        stamp = time.time()
        async with self.transaction() as db:
            prior = await db.execute(select(s.tool_operations).where(self._where(operation)))
            row = prior.mappings().one_or_none()
            if row is not None:
                if (row["input_digest"], row["tool"], row["contract_version"]) != (
                    digest,
                    tool,
                    version,
                ):
                    raise ValueError("Operation ID already bound to different input/tool/contract")
                return dict(row)
            await db.execute(
                insert(s.tool_runs)
                .values(
                    workspace_id=self.workspace,
                    run_id=run,
                    session_id=session,
                    scope=scope,
                    status="running",
                    predecessor=None,
                    updated=stamp,
                )
                .on_conflict_do_nothing()
            )
            bound = (
                (await db.execute(select(s.tool_runs).where(s.tool_runs.c.run_id == run)))
                .mappings()
                .one()
            )
            if (bound["workspace_id"], bound["session_id"]) != (self.workspace, session):
                raise ValueError("Run belongs to another session/workspace")
            await db.execute(
                update(s.tool_runs)
                .where(s.tool_runs.c.run_id == run)
                .values(status="running", updated=stamp)
            )
            await db.execute(
                insert(s.tool_steps)
                .values(
                    run_id=run,
                    step_id=call,
                    status="prepared",
                    updated=stamp,
                )
                .on_conflict_do_nothing()
            )
            await db.execute(
                insert(s.tool_operations).values(
                    operation_id=operation,
                    workspace_id=self.workspace,
                    run_id=run,
                    session_id=session,
                    scope=scope,
                    step_id=call,
                    call_id=call,
                    tool=tool,
                    contract_version=version,
                    input_digest=digest,
                    effect=effect,
                    status="prepared",
                    attempts=0,
                    created=stamp,
                    updated=stamp,
                    idempotency_key=operation,
                    resources=resources,
                )
            )
            return await self._row(db, operation)

    async def get(self, operation: str, *, session: str, scope: str) -> dict[str, Any]:
        async with self.database.transaction() as db:
            row = (
                (
                    await db.execute(
                        select(s.tool_operations).where(
                            self._where(operation),
                            s.tool_operations.c.session_id == session,
                            s.tool_operations.c.scope == scope,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ValueError("Unknown operation in this session/workspace")
            return dict(row)

    async def unresolved_conflicts(self, operation: str) -> list[str]:
        # This is an optimistic diagnostic read. claim() rechecks all conflicts
        # under the workspace lock before any effect can begin; no write/row lock
        # is needed for this frequently empty preflight check.
        async with self.database.transaction() as db:
            row = await self._row(db, operation, lock=False)
            others = (
                await db.execute(
                    select(s.tool_operations).where(
                        s.tool_operations.c.workspace_id == self.workspace,
                        s.tool_operations.c.status.in_(["uncertain", "partial"]),
                    )
                )
            ).mappings()
            return [
                other["operation_id"] for other in others if _operation_conflicts(row, dict(other))
            ]

    async def list_unresolved(self, *, session: str | None = None) -> list[dict[str, Any]]:
        """Read diagnostic receipts without claiming any resources or changing owners."""
        async with self.database.transaction() as db:
            query = select(s.tool_operations).where(
                s.tool_operations.c.workspace_id == self.workspace,
                s.tool_operations.c.status.in_(["uncertain", "partial"]),
            )
            if session is not None:
                query = query.where(s.tool_operations.c.session_id == session)
            return [dict(row) for row in (await db.execute(query)).mappings()]

    async def reconcile(
        self,
        operation: str,
        *,
        session: str,
        outcome: str,
        evidence: str,
        result_ref: str | None = None,
    ) -> None:
        """Trusted host resolution; never called automatically by the Agent.

        No-effect verification releases resource reservations but does not retry.
        Confirmed success requires a matching, hash-verified successful receipt.
        """
        if outcome not in {"no_effect", "succeeded"} or not evidence.strip():
            raise ValueError("Explicit outcome and reconciliation evidence are required")
        if outcome == "no_effect" and result_ref is not None:
            raise ValueError("No-effect resolution cannot publish a success receipt")
        if outcome == "succeeded":
            if not result_ref:
                raise ValueError("Confirmed success requires a verified result receipt")
            from researchx.storage.content import read_object
            from researchx.engine.messages import ToolResultBlock

            receipt = ToolResultBlock.model_validate_json(
                await read_object(self.database, self.workspace, result_ref)
            )
            if (
                receipt.is_error
                or receipt.result_metadata.get("status") != "success"
                or receipt.result_metadata.get("operation_id") != operation
            ):
                raise ValueError("Successful receipt must match the operation")
        async with self.transaction() as db:
            row = await self._row(db, operation)
            if row["session_id"] != session:
                raise ValueError("Operation belongs to another session")
            if row["status"] not in {"uncertain", "partial"}:
                raise ValueError("Only unresolved operations may be reconciled")
            if outcome == "succeeded" and receipt.tool_use_id != row["call_id"]:
                raise ValueError("Successful receipt must match the tool call")
            status = "failed" if outcome == "no_effect" else "succeeded"
            await db.execute(
                update(s.tool_operations)
                .where(self._where(operation))
                .values(
                    status=status,
                    reconciliation=json.dumps(
                        {
                            "outcome": outcome,
                            "evidence": evidence.strip(),
                            "verified_at": time.time(),
                        },
                        ensure_ascii=False,
                    ),
                    error_code="verified_no_effect" if outcome == "no_effect" else None,
                    result_ref=result_ref or row["result_ref"],
                    lease_until=None,
                    updated=time.time(),
                )
            )
            await db.execute(
                update(s.tool_attempts)
                .where(
                    s.tool_attempts.c.operation_id == operation,
                    s.tool_attempts.c.status.in_(["uncertain", "partial"]),
                )
                .values(status=status, updated=time.time())
            )
            await self._step(db, row, status)

    async def _attempt(self, db: AsyncSession, row: dict[str, Any]) -> str:
        attempt, stamp = uuid4().hex, time.time()
        await db.execute(
            insert(s.tool_attempts).values(
                attempt_id=attempt,
                operation_id=row["operation_id"],
                number=row["attempts"] + 1,
                status="running",
                created=stamp,
                updated=stamp,
            )
        )
        await db.execute(
            update(s.tool_operations)
            .where(self._where(row["operation_id"]))
            .values(
                attempts=row["attempts"] + 1,
                updated=stamp,
            )
        )
        return attempt

    async def claim(self, operation: str, owner: str) -> str | None:
        async with self.transaction() as db:
            row = await self._row(db, operation)
            if row["status"] != "prepared":
                return None
            conflicts = await db.execute(
                select(s.tool_operations).where(
                    s.tool_operations.c.workspace_id == self.workspace,
                    s.tool_operations.c.status.in_(["running", "uncertain", "partial"]),
                )
            )
            if any(_operation_conflicts(row, dict(other)) for other in conflicts.mappings()):
                return None
            await db.execute(
                update(s.tool_operations)
                .where(self._where(operation))
                .values(
                    status="running",
                    owner=owner,
                    pid=os.getpid(),
                    host_id=self.host,
                    lease_until=time.time() + 3600,
                )
            )
            await self._step(db, row, "running")
            return await self._attempt(db, row)

    async def next_attempt(self, operation: str, *, owner: str) -> str:
        async with self.transaction() as db:
            row = await self._row(db, operation)
            if row["owner"] != owner or row["status"] != "running":
                raise ValueError("Only the active owner may retry")
            await db.execute(
                update(s.tool_attempts)
                .where(
                    s.tool_attempts.c.operation_id == operation,
                    s.tool_attempts.c.status == "running",
                )
                .values(status="failed", updated=time.time())
            )
            return await self._attempt(db, row)

    async def _step(self, db: AsyncSession, row: dict[str, Any], status: str) -> None:
        await db.execute(
            update(s.tool_steps)
            .where(
                s.tool_steps.c.run_id == row["run_id"],
                s.tool_steps.c.step_id == row["step_id"],
            )
            .values(status=status, updated=time.time())
        )
        states = set(
            (
                await db.scalars(
                    select(s.tool_operations.c.status).where(
                        s.tool_operations.c.workspace_id == self.workspace,
                        s.tool_operations.c.run_id == row["run_id"],
                    )
                )
            ).all()
        )
        run_status = (
            "running"
            if states & {"running", "prepared"}
            else "blocked"
            if states & {"uncertain", "partial", "blocked"}
            else "failed"
            if "failed" in states
            else "cancelled"
            if "cancelled" in states
            else "succeeded"
        )
        await db.execute(
            update(s.tool_runs)
            .where(
                s.tool_runs.c.run_id == row["run_id"],
                s.tool_runs.c.workspace_id == self.workspace,
            )
            .values(status=run_status, updated=time.time())
        )

    async def settle(
        self,
        operation: str,
        status: str,
        *,
        owner: str | None = None,
        error_code: str | None = None,
        result_ref: str | None = None,
        external_request_id: str | None = None,
        evidence: str | None = None,
    ) -> None:
        if status not in STATES:
            raise ValueError("Invalid operation status")
        async with self.transaction() as db:
            row = await self._row(db, operation)
            if row["status"] == status:
                if result_ref is not None and row["result_ref"] != result_ref:
                    raise ValueError("Settled operation has a different result")
                return
            if status not in EDGES[row["status"]]:
                raise ValueError(f"Illegal operation transition {row['status']} -> {status}")
            if row["status"] in {"uncertain", "partial"} and not evidence:
                raise ValueError("Reconciliation evidence is required")
            if row["owner"] and row["owner"] != owner and not evidence:
                raise ValueError("Operation is owned by another executor")
            await db.execute(
                update(s.tool_operations)
                .where(self._where(operation))
                .values(
                    status=status,
                    error_code=error_code,
                    result_ref=result_ref or row["result_ref"],
                    external_request_id=external_request_id or row["external_request_id"],
                    reconciliation=evidence,
                    updated=time.time(),
                    lease_until=None,
                )
            )
            await db.execute(
                update(s.tool_attempts)
                .where(
                    s.tool_attempts.c.operation_id == operation,
                    s.tool_attempts.c.status == "running",
                )
                .values(status=status, updated=time.time())
            )
            await self._step(db, row, status)

    async def retry_failed(self, operation: str, contract: Any, *, verified_no_effect: str) -> None:
        if not verified_no_effect or contract.retry_mode == "never":
            raise ValueError("Recovery requires evidence and a retryable contract")
        async with self.transaction() as db:
            row = await self._row(db, operation)
            if row["status"] != "failed" or row["attempts"] >= contract.max_attempts:
                raise ValueError("Operation is not eligible for another attempt")
            if (row["tool"], row["contract_version"]) != (contract.name, contract.version):
                raise ValueError("Recovery contract mismatch")
            await db.execute(
                update(s.tool_operations)
                .where(self._where(operation))
                .values(
                    status="prepared",
                    owner=None,
                    pid=None,
                    host_id=None,
                    reconciliation=verified_no_effect,
                    updated=time.time(),
                )
            )
            await self._step(db, row, "prepared")

    async def recover(self, *, session: str, scope: str) -> list[dict[str, Any]]:
        async with self.transaction() as db:
            rows = (
                (
                    await db.execute(
                        select(s.tool_operations).where(
                            s.tool_operations.c.workspace_id == self.workspace,
                            s.tool_operations.c.session_id == session,
                            (s.tool_operations.c.scope == scope)
                            | s.tool_operations.c.scope.startswith(
                                scope + ":child:", autoescape=True
                            ),
                            s.tool_operations.c.status == "running",
                        )
                    )
                )
                .mappings()
                .all()
            )
            for item in rows:
                row = dict(item)
                # Unknown/remote host owners require explicit reconciliation. Lease expiration
                # alone cannot prove a remote process stopped or a write did not happen.
                if row["host_id"] != self.host or _alive(row["pid"]):
                    continue
                status = "failed" if row["effect"] == "read_only" else "uncertain"
                await db.execute(
                    update(s.tool_operations)
                    .where(self._where(row["operation_id"]))
                    .values(
                        status=status,
                        error_code="owner_lost",
                        updated=time.time(),
                        reconciliation="no_write_effect" if status == "failed" else "required",
                    )
                )
                await db.execute(
                    update(s.tool_attempts)
                    .where(
                        s.tool_attempts.c.operation_id == row["operation_id"],
                        s.tool_attempts.c.status == "running",
                    )
                    .values(status=status, updated=time.time())
                )
                await self._step(db, row, status)
            return [
                dict(row)
                for row in (
                    await db.execute(
                        select(s.tool_operations).where(
                            s.tool_operations.c.workspace_id == self.workspace,
                            s.tool_operations.c.session_id == session,
                            s.tool_operations.c.scope == scope,
                            s.tool_operations.c.status.in_(["uncertain", "partial"]),
                        )
                    )
                ).mappings()
            ]

    async def record_api_attempt(self, record: dict[str, Any], *, session: str) -> None:
        async with self.transaction() as db:
            existing = (
                (
                    await db.execute(
                        select(s.api_attempts)
                        .where(s.api_attempts.c.attempt_id == record["attempt_id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing:
                if (existing["workspace_id"], existing["session_id"], existing["request_id"]) != (
                    self.workspace,
                    session,
                    record["request_id"],
                ):
                    raise ValueError("API attempt ID is bound to another request")
                if existing["status"] != "running":
                    if existing["record"] == record:
                        return
                    raise ValueError("API attempt already settled")
            values = dict(
                workspace_id=self.workspace,
                session_id=session,
                attempt_id=record["attempt_id"],
                request_id=record["request_id"],
                record=record,
                status=record["status"],
                updated=record.get("finished", record.get("started", time.time())),
            )
            stmt = insert(s.api_attempts).values(**values)
            await db.execute(stmt.on_conflict_do_update(index_elements=["attempt_id"], set_=values))

    async def prune_audit(self, *, before: float, limit: int = 1000) -> dict[str, int]:
        if not 1 <= limit <= 10000:
            raise ValueError("Audit prune limit must be 1..10000")
        async with self.transaction() as db:
            api = list(
                (
                    await db.scalars(
                        select(s.api_attempts.c.attempt_id)
                        .where(
                            s.api_attempts.c.workspace_id == self.workspace,
                            s.api_attempts.c.status.in_(["succeeded", "failed", "cancelled"]),
                            s.api_attempts.c.updated < before,
                            s.api_attempts.c.record["usage_status"].astext == "reported",
                        )
                        .order_by(s.api_attempts.c.updated)
                        .limit(limit)
                    )
                ).all()
            )
            await db.execute(delete(s.api_attempts).where(s.api_attempts.c.attempt_id.in_(api)))
            attempts = list(
                (
                    await db.scalars(
                        select(s.tool_attempts.c.attempt_id)
                        .join(
                            s.tool_operations,
                            s.tool_operations.c.operation_id == s.tool_attempts.c.operation_id,
                        )
                        .where(
                            s.tool_operations.c.workspace_id == self.workspace,
                            s.tool_operations.c.status.in_(["succeeded", "failed", "cancelled"]),
                            s.tool_operations.c.updated < before,
                            s.tool_attempts.c.updated < before,
                            s.tool_attempts.c.status != "running",
                        )
                        .order_by(s.tool_attempts.c.updated)
                        .limit(limit - len(api))
                    )
                ).all()
            )
            await db.execute(
                delete(s.tool_attempts).where(s.tool_attempts.c.attempt_id.in_(attempts))
            )
            return {"api_attempts": len(api), "tool_attempts": len(attempts)}


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _conflicts(a: dict[str, list[str]], b: dict[str, list[str]]) -> bool:
    def intersects(xs: list[str], ys: list[str]) -> bool:
        return any(
            x == "*"
            or y == "*"
            or x == y
            or x.startswith(y.rstrip("/") + "/")
            or y.startswith(x.rstrip("/") + "/")
            for x in xs
            for y in ys
        )

    return intersects(a["write"], b["read"] + b["write"]) or intersects(
        b["write"], a["read"] + a["write"]
    )


def _operation_conflicts(a: dict[str, Any], b: dict[str, Any]) -> bool:
    # A trusted dispatch child starts only after the parent's PRE hooks finish.
    # The parent awaits its children, so retaining its orchestration reservation
    # against those exact children would deadlock. Siblings still conflict normally.
    def dispatch_parent(parent: dict[str, Any], child: dict[str, Any]) -> bool:
        return bool(
            parent["tool"] == "dispatch_subagents"
            and parent["status"] == "running"
            and child["resources"].get("parent_operation") == [parent["operation_id"]]
        )

    if dispatch_parent(a, b) or dispatch_parent(b, a):
        return False

    # Unresolved writes prohibit overlapping mutations, not diagnostics. A PRE
    # effect changes the ledger effect to mixed and therefore cannot use this.
    if (a["status"] in {"uncertain", "partial"} and b["effect"] == "read_only") or (
        b["status"] in {"uncertain", "partial"} and a["effect"] == "read_only"
    ):
        return False

    # Research control mutates a session's relational records, not another session's
    # files. Previously each ResearchStore had its own ledger; sharing a database
    # must not turn one interrupted research session into a workspace-wide outage.
    def session_control(row: dict[str, Any]) -> bool:
        return (
            row["tool"] in {"research_memory", "research_project", "planner", "replanner"}
            and row["effect"] != "mixed"
            and row["resources"] == {"read": [], "write": ["research.control"]}
        )

    if a["session_id"] != b["session_id"] and (session_control(a) or session_control(b)):
        return False
    return _conflicts(a["resources"], b["resources"])


def operation_identity(workspace: str, session: str, scope: str, call: str) -> str:
    return hashlib.sha256(json.dumps([workspace, session, scope, call]).encode()).hexdigest()
