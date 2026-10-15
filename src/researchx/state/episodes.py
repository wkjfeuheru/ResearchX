"""Bounded, session-scoped episode indexes over the existing relational tables.

This is a read interface. The storage migration will change its scope to the
business workspace; until then callers must not treat a directory hash as one.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, case, cast, literal, or_, select, String, union_all

from researchx.state.errors import ResearchError
from researchx.storage import schema as s
from researchx.storage.database import current_database


EPISODE_TABLES = {
    "source": s.entities["sources"],
    "evidence": s.entities["evidence_pool"],
    "reasoning": s.entities["reasoning_chain"],
    "conclusion": s.entities["conclusions"],
    "conflict": s.entities["conflicts"],
    "arbitration": s.entities["arbitrations"],
    "artifact": s.entities["artifacts"],
}

READ_TABLES = {
    "task_context": s.entities["task_context"],
    "plan": s.entities["plans"],
    **EPISODE_TABLES,
}

PROVENANCE_FIELDS = frozenset(
    {
        "source_id",
        "corrects_source_id",
        "evidence_id",
        "evidence_ids",
        "supporting_evidence_ids",
        "input_evidence_ids",
        "additional_evidence_ids",
        "assumption_evidence_ids",
        "evidence_versions",
        "step_ids",
        "prior_step_ids",
        "verification_step_id",
        "calculation_step_id",
        "conclusion_ids",
        "finding_ids",
        "input_artifact_ids",
        "supersedes",
        "reused_evidence_ids",
        "artifact_ids",
    }
)

PROVENANCE_TARGETS = {
    "source_id": "source",
    "corrects_source_id": "source",
    "evidence_id": "evidence",
    "evidence_ids": "evidence",
    "supporting_evidence_ids": "evidence",
    "input_evidence_ids": "evidence",
    "additional_evidence_ids": "evidence",
    "assumption_evidence_ids": "evidence",
    "reused_evidence_ids": "evidence",
    "evidence_versions": "evidence",
    "step_ids": "reasoning",
    "prior_step_ids": "reasoning",
    "verification_step_id": "reasoning",
    "calculation_step_id": "reasoning",
    "conclusion_ids": "conclusion",
    "input_artifact_ids": "artifact",
    "artifact_ids": "artifact",
}


def _provenance_refs(value: object, field: str = "") -> set[str]:
    if isinstance(value, str):
        return {value} if field in PROVENANCE_FIELDS else set()
    if isinstance(value, list):
        return set().union(*(_provenance_refs(item, field) for item in value))
    if isinstance(value, dict):
        result: set[str] = set()
        if field == "evidence_versions":
            result.update(key for key in value if isinstance(key, str))
        for key, item in value.items():
            result.update(_provenance_refs(item, key))
        return result
    return set()


def _typed_refs(value: object, field: str = "") -> set[tuple[str, str]]:
    target = PROVENANCE_TARGETS.get(field)
    if isinstance(value, str):
        return {(value, target)} if target else set()
    if isinstance(value, list):
        return set().union(*(_typed_refs(item, field) for item in value))
    if isinstance(value, dict):
        result = {(key, "evidence") for key in value if field == "evidence_versions"}
        for key, item in value.items():
            result.update(_typed_refs(item, key))
        return result
    return set()


@dataclass(frozen=True)
class EpisodeQuery:
    query: str = ""
    record_types: tuple[str, ...] = ()
    task_id: str | None = None
    status: str | None = None
    cursor: str | None = None
    limit: int = 20


class EpisodicRepository:
    def __init__(self, workspace_id: str, session_id: str) -> None:
        self.workspace_id = workspace_id
        self.session_id = session_id

    async def read_records(
        self, ids: list[str], *, strict: bool = False
    ) -> tuple[int, dict[str, tuple[str, dict[str, Any]]], dict[str, set[str]]]:
        """Read requested records and their provenance closure without a full snapshot."""
        if len(ids) > 100:
            raise ResearchError("单次最多读取 100 条研究记录")
        async with current_database().transaction() as db:
            revision = await db.scalar(
                select(s.research_sessions.c.revision)
                .where(
                    s.research_sessions.c.workspace_id == self.workspace_id,
                    s.research_sessions.c.session_id == self.session_id,
                )
                .with_for_update(read=True)
            )
            if revision is None:
                raise ResearchError("研究会话不存在")
            records: dict[str, tuple[str, dict[str, Any]]] = {}
            pending = set(ids)
            while pending:
                if len(records) + len(pending) > 5000:
                    raise ResearchError("研究记录引用过多，请缩小读取范围")
                branches = [
                    select(
                        literal(kind).label("kind"),
                        table.c.record_id.label("record_id"),
                        table.c.payload.label("payload"),
                    ).where(
                        table.c.workspace_id == self.workspace_id,
                        table.c.session_id == self.session_id,
                        table.c.record_id.in_(pending),
                    )
                    for kind, table in READ_TABLES.items()
                ]
                rows = (await db.execute(union_all(*branches))).mappings().all()
                if len({row["record_id"] for row in rows}) != len(rows):
                    raise ResearchError("研究记录 ID 在多个类型中重复，请按类型消除歧义")
                found = {row["record_id"]: (row["kind"], dict(row["payload"])) for row in rows}
                if any(key not in found and key not in records for key in ids):
                    raise ResearchError("未知的研究记录 ID")
                plan_ids = [key for key, (kind, _) in found.items() if kind == "plan"]
                if plan_ids:
                    tasks = (
                        (
                            await db.execute(
                                select(s.research_tasks.c.plan_id, s.research_tasks.c.payload)
                                .where(
                                    s.research_tasks.c.workspace_id == self.workspace_id,
                                    s.research_tasks.c.session_id == self.session_id,
                                    s.research_tasks.c.plan_id.in_(plan_ids),
                                )
                                .order_by(s.research_tasks.c.position)
                            )
                        )
                        .mappings()
                        .all()
                    )
                    for row in tasks:
                        found[row["plan_id"]][1].setdefault("tasks", []).append(row["payload"])
                records.update(found)
                pending = set().union(*(_provenance_refs(payload) for _, payload in found.values()))
                pending.difference_update(records)
            if strict and any(
                ref not in records
                for _, payload in records.values()
                for ref in _provenance_refs(payload)
            ):
                raise ResearchError("研究记录引用缺失，无法确认来源授权")
            if strict and any(
                records[ref][0] != target
                for _, payload in records.values()
                for ref, target in _typed_refs(payload)
            ):
                raise ResearchError("研究记录引用类型不匹配，无法确认来源授权")

        def sources_for(record_id: str, visited: set[str] | None = None) -> set[str]:
            visited = set() if visited is None else visited
            if record_id in visited or record_id not in records:
                return set()
            visited.add(record_id)
            kind, payload = records[record_id]
            if kind == "source":
                return {record_id}
            return set().union(*(sources_for(ref, visited) for ref in _provenance_refs(payload)))

        return revision, records, {key: sources_for(key) for key in ids}

    @staticmethod
    def _cursor_encode(payload: dict[str, Any]) -> str:
        return (
            base64.urlsafe_b64encode(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            )
            .decode()
            .rstrip("=")
        )

    @staticmethod
    def _cursor_decode(cursor: str) -> dict[str, Any]:
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, UnicodeDecodeError) as exc:
            raise ResearchError("无效的研究记录游标") from exc

    async def search(self, query: EpisodeQuery) -> dict[str, object]:
        if not 1 <= query.limit <= 100:
            raise ResearchError("研究记录查询 limit 必须在 1..100 之间")
        if len(query.query) > 200:
            raise ResearchError("研究记录查询关键词过长")
        types = tuple(dict.fromkeys(query.record_types or tuple(EPISODE_TABLES)))
        if any(item not in EPISODE_TABLES for item in types):
            raise ResearchError("未知的研究记录类型")
        filters = {
            "workspace": self.workspace_id,
            "session": self.session_id,
            "query": query.query,
            "types": types,
            "task": query.task_id,
            "status": query.status,
        }
        async with current_database().transaction() as db:
            # One repeatable-read statement would be ideal once the workspace
            # catalog owns a distinct revision. For the current schema, the
            # session revision is locked for a consistent read of both parts.
            revision = await db.scalar(
                select(s.research_sessions.c.revision)
                .where(
                    s.research_sessions.c.workspace_id == self.workspace_id,
                    s.research_sessions.c.session_id == self.session_id,
                )
                .with_for_update(read=True)
            )
            if revision is None:
                raise ResearchError("研究会话不存在")
            after: list[str] | None = None
            if query.cursor:
                saved = self._cursor_decode(query.cursor)
                if saved.get("filters") != {**filters, "types": list(types)}:
                    raise ResearchError("研究记录游标与查询条件不匹配")
                if saved.get("revision") != revision:
                    raise ResearchError("研究记录游标已过期，请重新检索")
                after = saved.get("after")
                if (
                    not isinstance(after, list)
                    or len(after) != 2
                    or not all(isinstance(value, str) for value in after)
                ):
                    raise ResearchError("无效的研究记录游标")
            branches = []
            for record_type in types:
                table = EPISODE_TABLES[record_type]
                status = (
                    case((table.c.payload["stale"].as_boolean().is_(True), "stale"), else_="active")
                    if record_type == "artifact"
                    else table.c.status
                )
                conditions = [
                    table.c.workspace_id == self.workspace_id,
                    table.c.session_id == self.session_id,
                ]
                if query.task_id:
                    conditions.append(table.c.task_id == query.task_id)
                if query.status:
                    conditions.append(status == query.status)
                elif record_type == "artifact":
                    conditions.append(status != "stale")
                elif record_type in {"evidence", "conclusion"}:
                    conditions.append(or_(status.is_(None), status != "retracted"))
                if query.query:
                    conditions.append(
                        cast(table.c.payload, String).contains(query.query, autoescape=True)
                    )
                branches.append(
                    select(
                        literal(record_type).label("record_type"),
                        table.c.record_id.label("record_id"),
                        table.c.task_id.label("task_id"),
                        status.label("status"),
                        table.c.revision.label("version"),
                    ).where(and_(*conditions))
                )
            catalog = union_all(*branches).subquery()
            statement = select(catalog)
            if after:
                statement = statement.where(
                    or_(
                        catalog.c.record_type > after[0],
                        and_(catalog.c.record_type == after[0], catalog.c.record_id > after[1]),
                    )
                )
            rows = (
                (
                    await db.execute(
                        statement.order_by(catalog.c.record_type, catalog.c.record_id).limit(
                            query.limit + 1
                        )
                    )
                )
                .mappings()
                .all()
            )
        page = rows[: query.limit]
        items = [
            {
                "record_type": row["record_type"],
                "id": row["record_id"],
                "task_id": row["task_id"],
                "status": row["status"] or "active",
                "version": row["version"] or 1,
                # The model may see this index without having source-use permission.
                # Detailed text remains behind the checked read(ids) action.
                "summary": f"{row['record_type']}:{row['record_id']}",
            }
            for row in page
        ]
        next_cursor = None
        if len(rows) > query.limit and page:
            next_cursor = self._cursor_encode(
                {
                    "filters": {**filters, "types": list(types)},
                    "revision": revision,
                    "after": [page[-1]["record_type"], page[-1]["record_id"]],
                }
            )
        return {"items": items, "next_cursor": next_cursor, "revision": revision}

    async def context_summary(self, *, max_items: int = 12) -> dict[str, object]:
        """Fetch control fields and a small index without loading full history."""
        if not 0 <= max_items <= 100:
            raise ValueError("max_items must be in 0..100")
        async with current_database().transaction() as db:
            header = (
                (
                    await db.execute(
                        select(s.research_sessions.c.revision, s.research_sessions.c.research_state)
                        .where(
                            s.research_sessions.c.workspace_id == self.workspace_id,
                            s.research_sessions.c.session_id == self.session_id,
                        )
                        .with_for_update(read=True)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if header is None:
                raise ResearchError("研究会话不存在")
            state = header["research_state"] or {}
            project = await db.scalar(
                select(s.entities["project"].c.payload).where(
                    s.entities["project"].c.workspace_id == self.workspace_id,
                    s.entities["project"].c.session_id == self.session_id,
                )
            )
            objective = None
            if project:
                objective = await db.scalar(
                    select(s.entities["objectives"].c.payload)
                    .where(
                        s.entities["objectives"].c.workspace_id == self.workspace_id,
                        s.entities["objectives"].c.session_id == self.session_id,
                    )
                    .order_by(s.entities["objectives"].c.position.desc())
                    .limit(1)
                )
            task = None
            if state.get("current_task_id"):
                task = await db.scalar(
                    select(s.research_tasks.c.payload).where(
                        s.research_tasks.c.workspace_id == self.workspace_id,
                        s.research_tasks.c.session_id == self.session_id,
                        s.research_tasks.c.record_id == state["current_task_id"],
                    )
                )
            # Only indexed columns are selected. Never materialize payloads or
            # the operation/history tables for automatic recall.
            branches = []
            for kind, table in (
                ("artifact", s.entities["artifacts"]),
                ("conclusion", s.entities["conclusions"]),
                ("evidence", s.entities["evidence_pool"]),
            ):
                status = (
                    case((table.c.payload["stale"].as_boolean().is_(True), "stale"), else_="active")
                    if kind == "artifact"
                    else table.c.status
                )
                branches.append(
                    select(
                        literal(kind).label("kind"),
                        table.c.record_id.label("id"),
                        table.c.task_id.label("task_id"),
                        status.label("status"),
                        table.c.revision.label("version"),
                        table.c.position.label("position"),
                    ).where(
                        table.c.workspace_id == self.workspace_id,
                        table.c.session_id == self.session_id,
                    )
                )
            catalog = union_all(*branches).subquery()
            items = (
                (
                    await db.execute(
                        select(catalog)
                        .where(
                            or_(
                                catalog.c.status.is_(None),
                                catalog.c.status.not_in(("retracted", "stale")),
                            )
                        )
                        .order_by(
                            (catalog.c.task_id == state.get("current_task_id")).desc()
                            if state.get("current_task_id")
                            else catalog.c.position.desc(),
                            catalog.c.position.desc(),
                            catalog.c.kind,
                            catalog.c.id,
                        )
                        .limit(max_items)
                    )
                )
                .mappings()
                .all()
                if max_items
                else []
            )
        return {
            "workspace_id": self.workspace_id,
            "session_id": self.session_id,
            "project_id": project.get("id") if project else None,
            "revision": header["revision"],
            "objective": str((objective or {}).get("subject", ""))[:160],
            "requirements": [
                str(item)[:160] for item in (objective or {}).get("requirements", [])[:3]
            ],
            "deliverables": [
                str(item)[:160] for item in (objective or {}).get("deliverables", [])[:3]
            ],
            "feedback": [str(item)[:160] for item in (project or {}).get("feedback", [])[-3:]],
            "current_task": {
                "id": state.get("current_task_id"),
                "title": str((task or {}).get("title", ""))[:160],
                "status": (task or {}).get("status"),
            }
            if state.get("current_task_id")
            else None,
            "unresolved": [str(item)[:160] for item in state.get("unresolved", [])[:3]],
            "next_step": str(state.get("next_step", ""))[:160],
            "items": [
                {
                    "id": row["id"],
                    "type": row["kind"],
                    "task_id": row["task_id"],
                    "status": row["status"] or "active",
                    "version": row["version"] or 1,
                }
                for row in items
            ],
        }
