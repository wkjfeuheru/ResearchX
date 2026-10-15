"""Web and CLI conversation rows share one PostgreSQL representation."""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from researchx.engine.messages import ConversationMessage, sanitize_conversation_messages
from researchx.storage import schema as s
from researchx.storage.database import Database, workspace_id
from researchx.storage.content import persist_object, read_object
from researchx.storage.research_records import ensure_session, scope


class ConversationRecords:
    def __init__(self, database: Database, cwd: str) -> None:
        from pathlib import Path

        self.database = database
        self.cwd = str(Path(cwd).resolve())
        self.workspace = workspace_id(self.cwd)

    async def _store_message(self, message: ConversationMessage, channel: str) -> dict[str, Any]:
        payload = message.model_dump(
            mode="json", exclude={"reasoning_content"} if channel == "subagent" else set()
        )
        for block in payload["content"]:
            field = {"image": "data", "text": "text", "tool_result": "content"}.get(block["type"])
            if field is None:
                continue
            body = block[field]
            if block["type"] == "image" or len(body.encode("utf-8")) > 65536:
                # Preserve the provider's original encoding exactly; binary/large
                # attachment bodies must not be duplicated in message JSONB.
                block[f"{field}_ref"] = await persist_object(
                    self.database, self.workspace, body.encode("utf-8")
                )
                del block[field]
        return payload

    async def _load_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        for block in payload["content"]:
            field = {"image": "data", "text": "text", "tool_result": "content"}.get(block["type"])
            if field is not None and f"{field}_ref" in block:
                reference = block.pop(f"{field}_ref")
                if not isinstance(reference, str):
                    raise ValueError("Invalid message content reference")
                block[field] = (await read_object(self.database, self.workspace, reference)).decode(
                    "utf-8"
                )
        return payload

    async def write(self, record: dict[str, Any], *, channel: str) -> None:
        from researchx.services.sessions.storage import _persistable_tool_metadata

        session = record["session_id"]
        if record.get("cwd", self.cwd) != self.cwd:
            raise ValueError("Conversation belongs to another workspace")
        messages = sanitize_conversation_messages(
            [ConversationMessage.model_validate(item) for item in record.get("messages", [])]
        )
        payload = {
            key: value
            for key, value in record.items()
            if key not in {"messages", "research_progress"}
        }
        payload["tool_metadata"] = _persistable_tool_metadata(record.get("tool_metadata"))
        payload["message_count"] = len(messages)
        async with self.database.transaction() as db:
            parent = record.get("parent_session_id")
            if parent:
                if parent == session or channel not in {"subagent", "context_archive"}:
                    raise ValueError("Invalid parent conversation relationship")
                await ensure_session(db, self.workspace, parent, self.cwd)
            await ensure_session(db, self.workspace, session, self.cwd)
            await db.execute(
                select(s.research_sessions.c.revision)
                .where(scope(s.research_sessions, self.workspace, session))
                .with_for_update()
            )
            if parent:
                previous_parent = await db.scalar(
                    select(s.research_sessions.c.parent_session_id).where(
                        scope(s.research_sessions, self.workspace, session)
                    )
                )
                if previous_parent not in {None, parent}:
                    raise ValueError("Child conversation cannot change parent")
                parent_parent = await db.scalar(
                    select(s.research_sessions.c.parent_session_id).where(
                        scope(s.research_sessions, self.workspace, parent)
                    )
                )
                if parent_parent is not None and channel == "subagent":
                    raise ValueError("Nested child conversations are not permitted")
                await db.execute(
                    update(s.research_sessions)
                    .where(scope(s.research_sessions, self.workspace, session))
                    .values(parent_session_id=parent)
                )
            values = {
                "workspace_id": self.workspace,
                "session_id": session,
                "channel": channel,
                "updated_at": record.get("updated_at", time.time()),
                "created_at": record.get("created_at", time.time()),
                "model": record.get("model", ""),
                "summary": record.get("summary", ""),
                "message_count": len(messages),
                "payload": payload,
            }
            stmt = insert(s.conversation_sessions).values(**values)
            await db.execute(
                stmt.on_conflict_do_update(
                    index_elements=["workspace_id", "session_id"],
                    set_={
                        key: value
                        for key, value in values.items()
                        if key not in {"workspace_id", "session_id", "created_at"}
                    },
                )
            )
            await db.execute(
                delete(s.conversation_messages).where(
                    scope(s.conversation_messages, self.workspace, session)
                )
            )
            if messages:
                await db.execute(
                    insert(s.conversation_messages),
                    [
                        {
                            "workspace_id": self.workspace,
                            "session_id": session,
                            "sequence": index,
                            "payload": await self._store_message(message, channel),
                        }
                        for index, message in enumerate(messages)
                    ],
                )

    async def load(self, session: str) -> dict[str, Any] | None:
        async with self.database.transaction() as db:
            # Share the writer's session lock: header and messages must be one snapshot.
            await db.execute(
                select(s.research_sessions.c.revision)
                .where(scope(s.research_sessions, self.workspace, session))
                .with_for_update(read=True)
            )
            payload = await db.scalar(
                select(s.conversation_sessions.c.payload).where(
                    scope(s.conversation_sessions, self.workspace, session)
                )
            )
            if payload is None:
                return None
            messages = (
                await db.scalars(
                    select(s.conversation_messages.c.payload)
                    .where(scope(s.conversation_messages, self.workspace, session))
                    .order_by(s.conversation_messages.c.sequence)
                )
            ).all()
            return {
                **payload,
                "messages": [await self._load_message(message) for message in messages],
                "message_count": len(messages),
            }

    async def list(self, *, channel: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        query = (
            select(s.conversation_sessions.c.session_id)
            .where(s.conversation_sessions.c.workspace_id == self.workspace)
            .order_by(
                s.conversation_sessions.c.updated_at.desc(), s.conversation_sessions.c.session_id
            )
        )
        if channel is not None:
            query = query.where(s.conversation_sessions.c.channel == channel)
        async with self.database.transaction() as db:
            ids = list((await db.scalars(query.limit(max(0, limit)))).all())
        results = []
        for session in ids:
            record = await self.load(session)
            if record is not None:
                results.append(record)
        return results

    async def delete(self, session: str) -> None:
        from researchx.storage.run_records import detach_conversation

        async with self.database.transaction() as db:
            await detach_conversation(db, self.workspace, session)
            deleted = await db.scalar(
                delete(s.conversation_sessions)
                .where(scope(s.conversation_sessions, self.workspace, session))
                .returning(s.conversation_sessions.c.session_id)
            )
            if deleted is None:
                raise FileNotFoundError(session)
            await db.execute(
                delete(s.conversation_messages).where(
                    scope(s.conversation_messages, self.workspace, session)
                )
            )
            # research_sessions is still the parent of project and episode rows
            # in the legacy schema. Keep it until those rows have been migrated
            # to workspace ownership; deleting it would cascade research data.
        # Immutable content is retained. Database deletion must never race a new content reference.


def subagent_session_id(parent: str, transcript_path: object) -> str:
    from pathlib import Path
    from hashlib import sha256
    import json

    path = Path(str(transcript_path))
    return (
        "child-"
        + sha256(
            json.dumps([parent, path.parent.parent.name, path.parent.name]).encode()
        ).hexdigest()
    )
