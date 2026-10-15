"""Durable run rows and workspace assets that outlive a single chat."""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from researchx.storage.database import current_database
from researchx.storage.schema import (
    project_asset_bindings,
    project_conversation_bindings,
    research_observations,
    research_run_prompts,
    research_runs,
)

UNFINISHED = ("queued", "running", "waiting_input")


async def record_accepted_run(
    db: AsyncSession,
    *,
    workspace_id: str,
    session_id: str,
    run_id: str,
    request_id: str,
    project_id: str = "",
    epoch: int = 1,
    status: str = "queued",
) -> None:
    now = time.time()
    await db.execute(
        insert(research_runs)
        .values(
            workspace_id=workspace_id,
            run_id=run_id,
            session_id=session_id,
            project_id=project_id or session_id,
            request_id=request_id,
            epoch=epoch,
            status=status,
            payload={},
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["workspace_id", "run_id"])
    )


async def set_run_status(workspace_id: str, run_id: str, status: str) -> None:
    async with current_database().transaction() as db:
        await db.execute(
            update(research_runs)
            .where(
                research_runs.c.workspace_id == workspace_id,
                research_runs.c.run_id == run_id,
            )
            .values(status=status, updated_at=time.time())
        )


async def run_status(workspace_id: str, run_id: str) -> str | None:
    async with current_database().transaction() as db:
        return await db.scalar(
            select(research_runs.c.status).where(
                research_runs.c.workspace_id == workspace_id,
                research_runs.c.run_id == run_id,
            )
        )


async def record_prompt(
    *,
    workspace_id: str,
    prompt_id: str,
    run_id: str,
    kind: str,
    epoch: int = 1,
) -> None:
    async with current_database().transaction() as db:
        await db.execute(
            insert(research_run_prompts)
            .values(
                workspace_id=workspace_id,
                prompt_id=prompt_id,
                run_id=run_id,
                epoch=epoch,
                kind=kind,
                status="open",
                payload={},
                created_at=time.time(),
            )
            .on_conflict_do_nothing(index_elements=["workspace_id", "prompt_id"])
        )


async def prompt_status(workspace_id: str, prompt_id: str) -> str | None:
    async with current_database().transaction() as db:
        return await db.scalar(
            select(research_run_prompts.c.status).where(
                research_run_prompts.c.workspace_id == workspace_id,
                research_run_prompts.c.prompt_id == prompt_id,
            )
        )


async def pause_unfinished_runs(workspace_id: str) -> int:
    """Mark queued, running and waiting runs paused. Does not call a model or tool."""
    now = time.time()
    async with current_database().transaction() as db:
        paused = await db.execute(
            update(research_runs)
            .where(
                research_runs.c.workspace_id == workspace_id,
                research_runs.c.status.in_(UNFINISHED),
            )
            .values(status="paused", updated_at=now)
            .returning(research_runs.c.run_id)
        )
        await db.execute(
            update(research_run_prompts)
            .where(
                research_run_prompts.c.workspace_id == workspace_id,
                research_run_prompts.c.status == "open",
            )
            .values(status="expired")
        )
        return len(paused.all())


async def save_observation(
    workspace_id: str,
    observation_id: str,
    *,
    version: int,
    content_hash: str,
    payload: dict[str, Any],
) -> None:
    async with current_database().transaction() as db:
        await db.execute(
            insert(research_observations)
            .values(
                workspace_id=workspace_id,
                observation_id=observation_id,
                version=version,
                content_hash=content_hash,
                payload=payload,
                created_at=time.time(),
            )
            .on_conflict_do_nothing(index_elements=["workspace_id", "observation_id", "version"])
        )


async def observation_exists(workspace_id: str, observation_id: str, version: int) -> bool:
    async with current_database().transaction() as db:
        found = await db.scalar(
            select(research_observations.c.observation_id).where(
                research_observations.c.workspace_id == workspace_id,
                research_observations.c.observation_id == observation_id,
                research_observations.c.version == version,
            )
        )
        return found is not None


async def bind_conversation(workspace_id: str, session_id: str, project_id: str) -> None:
    async with current_database().transaction() as db:
        await db.execute(
            insert(project_conversation_bindings)
            .values(
                workspace_id=workspace_id,
                session_id=session_id,
                project_id=project_id,
                created_at=time.time(),
            )
            .on_conflict_do_nothing(index_elements=["workspace_id", "session_id"])
        )


async def bind_asset(workspace_id: str, project_id: str, asset_id: str, asset_kind: str) -> None:
    async with current_database().transaction() as db:
        await db.execute(
            insert(project_asset_bindings)
            .values(
                workspace_id=workspace_id,
                project_id=project_id,
                asset_id=asset_id,
                asset_kind=asset_kind,
                payload={},
            )
            .on_conflict_do_nothing(index_elements=["workspace_id", "project_id", "asset_id"])
        )


async def project_can_cite(workspace_id: str, project_id: str, asset_id: str) -> bool:
    async with current_database().transaction() as db:
        found = await db.scalar(
            select(project_asset_bindings.c.asset_id).where(
                project_asset_bindings.c.workspace_id == workspace_id,
                project_asset_bindings.c.project_id == project_id,
                project_asset_bindings.c.asset_id == asset_id,
            )
        )
        return found is not None


async def conversation_project(workspace_id: str, session_id: str) -> str | None:
    async with current_database().transaction() as db:
        return await db.scalar(
            select(project_conversation_bindings.c.project_id).where(
                project_conversation_bindings.c.workspace_id == workspace_id,
                project_conversation_bindings.c.session_id == session_id,
            )
        )


async def detach_conversation(db: AsyncSession, workspace_id: str, session_id: str) -> None:
    """Drop the chat binding. Workspace observations and model runs stay."""
    await db.execute(
        delete(project_conversation_bindings).where(
            project_conversation_bindings.c.workspace_id == workspace_id,
            project_conversation_bindings.c.session_id == session_id,
        )
    )
