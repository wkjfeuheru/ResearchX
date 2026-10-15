"""Accepted requests and unsanitized execution logs; never schedule during recovery."""

from __future__ import annotations

import json
import time
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from researchx.engine.messages import ConversationMessage
from researchx.storage import schema as s
from researchx.storage.content import persist_object, read_object
from researchx.storage.database import current_database, workspace_id
from researchx.storage.research_records import ensure_session, scope as record_scope


class RequestConflict(ValueError):
    pass


class CheckpointError(RuntimeError):
    pass


class TurnJournal:
    def __init__(self, cwd: str, session_id: str, request_id: str = "") -> None:
        self.cwd = str(Path(cwd).resolve())
        self.workspace = workspace_id(self.cwd)
        self.session_id = session_id
        self.request_id = request_id
        self.database = current_database()

    def _where(self) -> Any:
        return record_scope(s.conversation_turns, self.workspace, self.session_id) & (
            s.conversation_turns.c.request_id == self.request_id
        )

    async def get(self) -> dict[str, Any] | None:
        async with self.database.transaction() as db:
            row = (
                (await db.execute(select(s.conversation_turns).where(self._where())))
                .mappings()
                .one_or_none()
            )
            return dict(row) if row else None

    def fingerprint(self, command: dict[str, Any]) -> str:
        return sha256(
            json.dumps(command, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()

    async def accept(
        self, command: dict[str, Any], effective_input: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        stamp = time.time()
        ack = {"accepted": True, "request_id": self.request_id}
        if command["type"] == "steer":
            ack["next_request_id"] = self.request_id
        async with self.database.transaction() as db:
            await ensure_session(db, self.workspace, self.session_id, self.cwd)
            inserted = await db.scalar(
                insert(s.conversation_turns)
                .values(
                    workspace_id=self.workspace,
                    session_id=self.session_id,
                    request_id=self.request_id,
                    command=deepcopy(command),
                    payload_digest=self.fingerprint(command),
                    digest_version=1,
                    message_id="msg_"
                    + sha256(
                        json.dumps([self.workspace, self.session_id, self.request_id]).encode()
                    ).hexdigest(),
                    effective_input=deepcopy(effective_input),
                    ack=ack,
                    status="accepted",
                    sequence=0,
                    created_at=stamp,
                    updated_at=stamp,
                )
                .on_conflict_do_nothing()
                .returning(s.conversation_turns.c.request_id)
            )
            row = await self.get()
            assert row is not None
            if row["digest_version"] != 1 or row["payload_digest"] != self.fingerprint(command):
                raise RequestConflict("请求 ID 已被不同命令使用")
            return row, inserted is not None

    async def set_status(self, status: str) -> None:
        async with self.database.transaction() as db:
            await db.execute(
                update(s.conversation_turns)
                .where(self._where())
                .values(status=status, updated_at=time.time())
            )

    async def save(
        self,
        *,
        phase: str,
        messages: list[ConversationMessage],
        scope: str | None = None,
        operations: list[dict[str, Any]] | None = None,
        projection: dict[str, Any] | None = None,
    ) -> None:
        try:
            # Copy before awaiting: concurrent producers cannot mutate a committed snapshot.
            raw = json.dumps(
                [m.model_dump(mode="json") for m in messages], ensure_ascii=False
            ).encode()
            async with self.database.transaction() as db:
                row = (
                    (
                        await db.execute(
                            select(s.conversation_turns).where(self._where()).with_for_update()
                        )
                    )
                    .mappings()
                    .one()
                )
                sequence = row["sequence"] + 1
                reference = await persist_object(self.database, self.workspace, raw)
                await db.execute(
                    insert(s.conversation_checkpoints).values(
                        workspace_id=self.workspace,
                        session_id=self.session_id,
                        request_id=self.request_id,
                        sequence=sequence,
                        phase=phase,
                        scope=scope or self.cwd,
                        messages_ref=reference.removeprefix("sha256:"),
                        operations=deepcopy(operations or []),
                        projection=deepcopy(projection or {}),
                        created_at=time.time(),
                    )
                )
                await db.execute(
                    update(s.conversation_turns)
                    .where(self._where())
                    .values(sequence=sequence, updated_at=time.time())
                )
        except Exception:
            raise CheckpointError("执行检查点保存失败；已停止后续执行") from None

    async def record_operation(self, operation: dict[str, Any], *, scope: str) -> None:
        try:
            async with self.database.transaction() as db:
                row = (
                    (
                        await db.execute(
                            select(s.conversation_checkpoints)
                            .where(
                                record_scope(
                                    s.conversation_checkpoints, self.workspace, self.session_id
                                ),
                                s.conversation_checkpoints.c.request_id == self.request_id,
                                s.conversation_checkpoints.c.scope == scope,
                            )
                            .order_by(s.conversation_checkpoints.c.sequence.desc())
                            .limit(1)
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    raise CheckpointError("工具执行意图缺失")
                messages = [
                    ConversationMessage.model_validate(m)
                    for m in json.loads(
                        await read_object(
                            self.database, self.workspace, "sha256:" + row["messages_ref"]
                        )
                    )
                ]
                await self.save(
                    phase="operation_prepared",
                    messages=messages,
                    scope=scope,
                    operations=[
                        {
                            key: operation[key]
                            for key in (
                                "operation_id",
                                "scope",
                                "call_id",
                                "input_digest",
                                "tool",
                                "contract_version",
                            )
                        }
                    ],
                )
        except Exception:
            raise CheckpointError("工具执行意图保存失败；已停止后续执行") from None

    async def recover(self, record: dict[str, Any]) -> dict[str, Any]:
        from researchx.engine.messages import sanitize_conversation_messages
        from researchx.services.execution.tool_execution import recover_messages

        async with self.database.transaction() as db:
            turns = (
                (
                    await db.execute(
                        select(s.conversation_turns)
                        .where(record_scope(s.conversation_turns, self.workspace, self.session_id))
                        .order_by(s.conversation_turns.c.created_at)
                    )
                )
                .mappings()
                .all()
            )
            changed = False
            rows = list(record.get("display_messages") or [])
            for turn in turns:
                if turn["status"] not in {"accepted", "running"}:
                    continue
                changed = True
                self.request_id = turn["request_id"]
                checkpoints = (
                    (
                        await db.execute(
                            select(s.conversation_checkpoints)
                            .where(
                                record_scope(
                                    s.conversation_checkpoints, self.workspace, self.session_id
                                ),
                                s.conversation_checkpoints.c.request_id == self.request_id,
                            )
                            .order_by(s.conversation_checkpoints.c.sequence)
                        )
                    )
                    .mappings()
                    .all()
                )
                main = [item for item in checkpoints if item["scope"] == self.cwd]
                if main and main[-1]["phase"] != "accepted":
                    from researchx.api.usage import UsageSnapshot
                    from researchx.engine.cost_tracker import CostTracker

                    baseline = next(
                        (
                            item["projection"].get("usage", {})
                            for item in main
                            if item["phase"] == "accepted"
                        ),
                        record.get("usage", {}),
                    )
                    usage = CostTracker(UsageSnapshot.model_validate(baseline))
                    for item in checkpoints:
                        if item["phase"] == "model_response" and item["projection"].get("usage"):
                            usage.add(UsageSnapshot.model_validate(item["projection"]["usage"]))
                    record["usage"] = usage.total.model_dump(mode="json")
                    raw = [
                        ConversationMessage.model_validate(m)
                        for m in json.loads(
                            await read_object(
                                self.database, self.workspace, "sha256:" + main[-1]["messages_ref"]
                            )
                        )
                    ]
                    operations = {op["call_id"]: op for item in main for op in item["operations"]}
                    raw = await recover_messages(
                        raw,
                        {"session_id": self.session_id, "operation_mappings": operations},
                        Path(self.cwd),
                        self.session_id,
                    )
                    record["messages"] = [
                        m.model_dump(mode="json") for m in sanitize_conversation_messages(raw)
                    ]
                    if main[-1]["projection"].get("display_messages"):
                        known_rows = {r["id"] for r in rows}
                        rows.extend(
                            r
                            for r in main[-1]["projection"]["display_messages"]
                            if r["id"] not in known_rows
                        )
                    from researchx.state.models import AnswerReceipt
                    from researchx.web.activity import describe_tool
                    from researchx.web.citations import render_web_answer
                    from researchx.web.redaction import Redactor

                    redactor = Redactor()
                    current_start = next(
                        (i + 1 for i, m in enumerate(raw) if m.message_id == turn["message_id"]),
                        len(raw),
                    )
                    current = raw[current_start:]
                    results = {
                        block.tool_use_id: block
                        for message in current
                        for block in message.content
                        if block.type == "tool_result"
                    }
                    known = {r["id"] for r in rows}
                    for message in current:
                        if message.role == "assistant" and message.text:
                            text = (
                                render_web_answer(cast(AnswerReceipt, message.research_citations))
                                if message.research_citations
                                else message.text
                            )
                            if not any(
                                r.get("turn_id") == self.request_id
                                and r["role"] == "assistant"
                                and r["text"] == text
                                for r in rows
                            ):
                                rows.append(
                                    {
                                        "id": message.message_id
                                        or "recovered_"
                                        + sha256(message.model_dump_json().encode()).hexdigest(),
                                        "role": "assistant",
                                        "text": redactor.clean(text),
                                        "turn_id": self.request_id,
                                        "turn_status": "stopped",
                                        "phase": "progress",
                                    }
                                )
                        for call in message.tool_uses:
                            row_id = self.request_id + ":" + call.id
                            if row_id in known:
                                continue
                            result = results.get(call.id)
                            rows.append(
                                {
                                    "id": row_id,
                                    "role": "activity",
                                    "text": "",
                                    "turn_id": self.request_id,
                                    "turn_status": "stopped",
                                    "status": "completed"
                                    if result and not result.is_error
                                    else "interrupted",
                                    "detail": "已恢复成功回执"
                                    if result and not result.is_error
                                    else "执行中断；恢复记录已保留，需要核对后继续",
                                    **describe_tool(call.name, redactor.clean(call.input)),
                                }
                            )
                            known.add(row_id)
                if not any(r["id"] == self.request_id for r in rows):
                    rows.append(
                        {
                            "id": self.request_id,
                            "role": "user",
                            "text": turn["command"]["text"].strip(),
                            "turn_id": self.request_id,
                            "turn_status": "stopped",
                        }
                    )
                if not any(m.get("message_id") == turn["message_id"] for m in record["messages"]):
                    record["messages"].append(
                        ConversationMessage.from_user_text(turn["effective_input"]["user_text"])
                        .model_copy(
                            update={
                                "message_id": turn["message_id"],
                                "context_origin": "user_input",
                            }
                        )
                        .model_dump(mode="json")
                    )
                await self.set_status("interrupted")
            if changed:
                for row in rows:
                    if row.get("turn_status") == "running":
                        row["turn_status"] = "stopped"
                    if row.get("status") == "running":
                        row["status"] = "interrupted"
                    if row.get("phase") == "pending":
                        row["phase"] = "progress"
                record["display_messages"] = rows
                from researchx.storage.conversations import ConversationRecords

                await ConversationRecords(self.database, self.cwd).write(record, channel="web")
            return record
