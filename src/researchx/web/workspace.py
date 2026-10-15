"""Web session coordination: connection ownership, request locks and recovery projections."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, cast
from fastapi import HTTPException
from researchx.state.errors import ResearchError
from researchx.state.store import ResearchStore
from researchx.web.storage import WebSessionBackend
from researchx.web.types import WebSessionRecord, BrowserRow
from researchx.web.session_view import session_view

if TYPE_CHECKING:
    from researchx.web.runtime import SessionController


class WebWorkspace:
    def __init__(self, cwd: str) -> None:
        self.cwd = str(Path(cwd).resolve())
        self.store = WebSessionBackend(self.cwd)
        self.lock = asyncio.Lock()
        self.connections: dict[str, SessionController] = {}
        self.active_runs: dict[str, SessionController] = {}
        self.execution_owner: str | None = None
        self.max_concurrent_runs = 2
        self.queue_capacity = 64
        self._queued: list[str] = []
        self.deleting: set[str] = set()
        self.file_operations: set[str] = set()

    def busy(self) -> bool:
        return any(controller.busy for controller in self.active_runs.values())

    def running_count(self) -> int:
        return sum(1 for controller in self.active_runs.values() if controller.holds_run_slot())

    def start_queued(self, finishing: SessionController | None = None) -> None:
        """Start the next accepted run once an execution slot is free."""
        running = sum(
            1
            for controller in self.active_runs.values()
            if controller.holds_run_slot() and controller is not finishing
        )
        while self._queued and running < self.max_concurrent_runs:
            session_id = self._queued.pop(0)
            connection = self.active_runs.get(session_id)
            request = connection.queued_request if connection is not None else None
            if connection is None or request is None or connection.busy:
                continue
            connection.queued_request = None
            connection.task = asyncio.create_task(connection.run(request))
            running += 1

    async def record(self, session_id: str) -> WebSessionRecord:
        try:
            if session_id in self.deleting or (await self.store.records.load(session_id)) is None:
                raise HTTPException(404, "会话不存在")
            connection = self.connections.get(session_id) or self.active_runs.get(session_id)
            live = connection is not None and connection.busy
            idle = not live
            if idle:
                (await ResearchStore(self.cwd, session_id).recover_pending_steers())
                (await ResearchStore(self.cwd, session_id).recover_investigations())
                from researchx.state.repository import ResearchRepository

                (await ResearchRepository(ResearchStore(self.cwd, session_id)).recover())
                from researchx.state.dispatch_audit import recover_dispatches

                research = ResearchStore(self.cwd, session_id)
                memory = await research.load()
                if memory.project is None:
                    root = research.directory / "delegation_workspace"
                    await recover_dispatches(research, None, root if root.is_dir() else None)

            record = await self.store.load_by_id(self.cwd, session_id)
            if record is not None:
                if idle:
                    from researchx.storage.turns import TurnJournal
                    from researchx.storage.database import current_database

                    async with current_database().transaction():
                        record = cast(
                            WebSessionRecord,
                            await TurnJournal(self.cwd, session_id).recover(dict(record)),
                        )
                # Acceptance is authoritative even if the process stopped
                # before the connection could save its display projection.
                pending = (await ResearchStore(self.cwd, session_id).load()).pending_steers
                rows = list(session_view(record)["messages"])
                recovered = False
                if idle:
                    # A persisted running projection can survive a process crash.
                    # Opening history must not claim it is still executing.
                    for row in rows:
                        if row.get("turn_status") == "running":
                            row["turn_status"] = "paused"
                            if row.get("phase") == "pending":
                                row["phase"] = "progress"
                            if row.get("status") == "running":
                                row["status"] = "interrupted"
                            recovered = True
                known = {row["id"] for row in rows}
                missing: list[BrowserRow] = [
                    {"id": key, "role": "user", "text": item["text"]}
                    for key, item in pending.items()
                    if key not in known
                ]
                if missing or recovered:
                    record["display_messages"] = rows + missing
                    (await self.store.write(record))
        except ResearchError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError:
            record = None
        if record is None:
            raise HTTPException(404, "会话不存在")
        return record
