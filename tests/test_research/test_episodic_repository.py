"""Behavior of bounded recall over the existing relational research records."""

import base64
import json

import pytest
from sqlalchemy import insert, update

from researchx.services.context.sources import ContextSnapshot, compose_research_context
from researchx.engine.messages import ConversationMessage, ToolResultBlock
from researchx.state.episodes import EpisodeQuery, EpisodicRepository
from researchx.state.errors import ResearchError
from researchx.state.models import (
    Evidence, ResearchArtifact, ResearchPlan, ResearchTask, SourceUsage, now,
)
from researchx.state.source_policy import (
    history_use_gap, image_use_allowed, request_policy_view,
)
from researchx.state.store import ResearchStore
from researchx.storage import schema as s
from researchx.storage.conversations import ConversationRecords
from researchx.storage.database import current_database
from researchx.tools.base import ToolExecutionContext
from researchx.tools.image_to_text_tool import ImageToTextTool, ImageToTextToolInput
from researchx.tools.research_memory_tool import ResearchMemoryInput, ResearchMemoryTool


async def _seed(store: ResearchStore, count: int = 25) -> None:
    await store.load()
    async with current_database().transaction() as db:
        await db.execute(
            insert(s.entities["evidence_pool"]),
            [
                {
                    "workspace_id": store.workspace_id,
                    "session_id": store.session_id,
                    "record_id": f"ev_{index:04d}",
                    "position": index,
                    "status": "retracted" if index == 0 else "pending",
                    "revision": 1,
                    "task_id": "task_one",
                    "payload": {"id": f"ev_{index:04d}", "statement": "盈利预测"},
                }
                for index in range(count)
            ],
        )


async def test_search_pages_and_expires_after_catalog_change(tmp_path):
    store = ResearchStore(tmp_path, "a" * 12)
    await _seed(store)
    repo = EpisodicRepository(store.workspace_id, store.session_id)
    first = await repo.search(EpisodeQuery(query="盈利预测", record_types=("evidence",), limit=10))
    assert len(first["items"]) == 10
    assert "ev_0000" not in {item["id"] for item in first["items"]}
    second = await repo.search(
        EpisodeQuery(query="盈利预测", record_types=("evidence",), limit=10, cursor=first["next_cursor"])
    )
    assert not {item["id"] for item in first["items"]} & {item["id"] for item in second["items"]}
    with pytest.raises(ResearchError, match="不匹配"):
        await repo.search(EpisodeQuery(record_types=("evidence",), limit=10, cursor=first["next_cursor"]))
    other = ResearchStore(tmp_path / "another", "a" * 12)
    await other.load()
    other_repo = EpisodicRepository(other.workspace_id, other.session_id)
    assert (await other_repo.search(EpisodeQuery(record_types=("evidence",))))["items"] == []
    with pytest.raises(ResearchError, match="不匹配"):
        await other_repo.search(
            EpisodeQuery(query="盈利预测", record_types=("evidence",), limit=10,
                         cursor=first["next_cursor"])
        )
    async with current_database().transaction() as db:
        await db.execute(
            update(s.research_sessions)
            .where(s.research_sessions.c.workspace_id == store.workspace_id,
                   s.research_sessions.c.session_id == store.session_id)
            .values(revision=s.research_sessions.c.revision + 1)
        )
    with pytest.raises(ResearchError, match="过期"):
        await repo.search(
            EpisodeQuery(query="盈利预测", record_types=("evidence",), limit=10,
                         cursor=first["next_cursor"])
        )


async def test_stale_artifacts_are_not_advertised_as_active(tmp_path):
    store = ResearchStore(tmp_path, "e" * 12)
    await store.load()
    async with current_database().transaction() as db:
        await db.execute(
            insert(s.entities["artifacts"]),
            [
                {"workspace_id": store.workspace_id, "session_id": store.session_id,
                 "record_id": "artifact_old", "position": 0, "payload": {"stale": True}},
                {"workspace_id": store.workspace_id, "session_id": store.session_id,
                 "record_id": "artifact_new", "position": 1, "payload": {"stale": False}},
            ],
        )
    repo = EpisodicRepository(store.workspace_id, store.session_id)
    active = await repo.search(EpisodeQuery(record_types=("artifact",)))
    assert [item["id"] for item in active["items"]] == ["artifact_new"]
    stale = await repo.search(EpisodeQuery(record_types=("artifact",), status="stale"))
    assert [item["id"] for item in stale["items"]] == ["artifact_old"]
    summary = await repo.context_summary()
    assert "artifact_old" not in {item["id"] for item in summary["items"]}


async def test_automatic_context_is_bounded_and_does_not_load_note_or_history(tmp_path, monkeypatch):
    store = ResearchStore(tmp_path, "b" * 12)
    await _seed(store, 100)
    monkeypatch.setattr(store, "load", lambda: pytest.fail("full aggregate loaded"))
    snapshot = await compose_research_context(
        ContextSnapshot(), store=store, model="gpt-4o", auto_index_max_items=12
    )
    packet = json.loads(snapshot.text.split("<research_memory>\n", 1)[1].split("\n</research_memory>", 1)[0])
    assert len(packet["items"]) == 12
    assert "盈利预测" not in snapshot.text
    assert packet["memory_path"] == "MEMORY.md"


async def test_model_search_and_empty_read_do_not_load_aggregate(tmp_path, monkeypatch):
    store = ResearchStore(tmp_path, "d" * 12)
    await _seed(store)
    monkeypatch.setattr(store, "load", lambda: pytest.fail("full aggregate loaded"))
    tool = ResearchMemoryTool()
    context = ToolExecutionContext(cwd=tmp_path, metadata={"research_store": store})
    for operation in ({"action": "search", "query": "盈利预测", "limit": 3}, {"action": "read"}):
        result = await tool.execute(ResearchMemoryInput.model_validate({"operation": operation}), context)
        assert not result.is_error
        assert json.loads(result.output)["revision"] >= 0


async def test_deleting_chat_keeps_research_sources(tmp_path):
    store = ResearchStore(tmp_path, "c" * 12)
    await store.capture(origin_id="user", kind="user", content="原始资料")
    conversations = ConversationRecords(current_database(), str(tmp_path))
    await conversations.write(
        {"session_id": store.session_id, "cwd": str(tmp_path), "messages": [],
         "created_at": 1.0, "updated_at": 1.0}, channel="web"
    )
    before = await store.load()
    await conversations.delete(store.session_id)
    assert await conversations.load(store.session_id) is None
    assert (await store.load()).sources == before.sources


async def test_read_source_tracks_revocation_in_tool_provenance(tmp_path, monkeypatch):
    store = ResearchStore(tmp_path, "f" * 12)
    source = await store.capture(
        origin_id="public-result", kind="web", content="资料正文",
        usage=SourceUsage(category="public", licence_status="pending", model_use_allowed=True),
    )
    monkeypatch.setattr(store, "load", lambda: pytest.fail("full aggregate loaded"))
    tool = ResearchMemoryTool()
    result = await tool.execute(
        ResearchMemoryInput.model_validate(
            {"operation": {"action": "read", "ids": [source.id], "include_content": True}}
        ),
        ToolExecutionContext(cwd=tmp_path, metadata={"research_store": store}),
    )
    assert not result.is_error and result.metadata["research_sources"] == [source.id]
    async with store.transaction():
        memory = await store._load()
        memory.sources[source.id].usage.model_use_allowed = False
        await store._save(memory, "revoke_source", {"source_id": source.id})
    message = ConversationMessage(
        role="user",
        content=[ToolResultBlock(
            tool_use_id="toolu_read", content=result.output, result_metadata=result.metadata,
        )],
    )
    policy_view, _ = await request_policy_view(
        store.workspace_id, store.session_id, [message], set()
    )
    assert history_use_gap(policy_view, [message]) is not None


async def test_plan_reused_evidence_is_checked_in_sparse_read(tmp_path, monkeypatch):
    store = ResearchStore(tmp_path, "1" * 12)
    source = await store.capture(origin_id="restricted", kind="web", content="limited")
    evidence = Evidence(id="evidence_reused", statement="restricted input", source_id=source.id,
                        collected_at=now())
    task = ResearchTask(id="task_reuse", title="Analyze")
    plan = ResearchPlan(id="plan_reuse", context_id="ctx", title="Plan", tasks=[task],
                        reused_evidence_ids=[evidence.id])
    plan_payload = plan.model_dump(mode="json")
    plan_payload.pop("tasks")
    await store.load()
    async with current_database().transaction() as db:
        await db.execute(insert(s.entities["evidence_pool"]).values(
            workspace_id=store.workspace_id, session_id=store.session_id,
            record_id=evidence.id, position=0, status=evidence.status,
            payload=evidence.model_dump(mode="json"),
        ))
        await db.execute(insert(s.entities["plans"]).values(
            workspace_id=store.workspace_id, session_id=store.session_id,
            record_id=plan.id, position=0, payload=plan_payload,
        ))
        await db.execute(insert(s.research_tasks).values(
            workspace_id=store.workspace_id, session_id=store.session_id,
            plan_id=plan.id, record_id=task.id, position=0, status=task.status,
            revision=task.task_revision, payload=task.model_dump(mode="json"),
        ))
    monkeypatch.setattr(store, "load", lambda: pytest.fail("full aggregate loaded"))
    with pytest.raises(ResearchError, match="来源授权"):
        await store.apply({"action": "read", "ids": [plan.id]}, model_access=True)
    result = await store.apply({"action": "read", "ids": [plan.id]})
    assert source.id in result["source_ids"]
    assert result["records"][0]["tasks"][0]["id"] == task.id


async def test_image_artifact_checks_assumption_sources(tmp_path):
    store = ResearchStore(tmp_path, "2" * 12)
    allowed = await store.capture(
        origin_id="public", kind="web", content="public",
        usage=SourceUsage(category="public", licence_status="pending", model_use_allowed=True),
    )
    restricted = await store.capture(origin_id="private", kind="web", content="private")
    user_source = await store.capture(origin_id="user-chart", kind="user", content="user data")
    digest, snapshot = await store.write_snapshot("chart image")
    main = Evidence(id="evidence_main", statement="main", source_id=allowed.id,
                    collected_at=allowed.collected_at)
    assumption = Evidence(id="evidence_assumption", statement="assumption",
                          source_id=restricted.id, collected_at=restricted.collected_at)
    artifact = ResearchArtifact(
        id="artifact_chart", task_id="task", task_revision=1, plan_revision=1,
        objective_revision=1, execution_id="execution", kind="chart", title="Chart",
        snapshot=snapshot, content_hash=digest, evidence_ids=[main.id],
        assumption_evidence_ids=[assumption.id],
    )
    async with current_database().transaction() as db:
        for index, evidence in enumerate((main, assumption)):
            await db.execute(insert(s.entities["evidence_pool"]).values(
                workspace_id=store.workspace_id, session_id=store.session_id,
                record_id=evidence.id, position=index, status=evidence.status,
                payload=evidence.model_dump(mode="json"),
            ))
        await db.execute(insert(s.entities["artifacts"]).values(
            workspace_id=store.workspace_id, session_id=store.session_id,
            record_id=artifact.id, position=0, content_hash=digest,
            payload=artifact.model_dump(mode="json"),
        ))
    view, approved = await request_policy_view(store.workspace_id, store.session_id, [], {digest})
    assert not image_use_allowed(view, digest, approved)
    blocked = await ImageToTextTool().execute(
        ImageToTextToolInput(image_data=base64.b64encode(b"chart image").decode()),
        ToolExecutionContext(cwd=tmp_path, metadata={"research_store": store}),
    )
    assert blocked.error_code == "source_use_not_authorized"

    public_digest, public_snapshot = await store.write_snapshot("public chart")
    public_artifact = artifact.model_copy(update={
        "id": "artifact_public_chart", "content_hash": public_digest,
        "snapshot": public_snapshot, "assumption_evidence_ids": [],
    })
    async with current_database().transaction() as db:
        await db.execute(insert(s.entities["artifacts"]).values(
            workspace_id=store.workspace_id, session_id=store.session_id,
            record_id=public_artifact.id, position=1, content_hash=public_digest,
            payload=public_artifact.model_dump(mode="json"),
        ))
    allowed_result = await ImageToTextTool().execute(
        ImageToTextToolInput(image_data=base64.b64encode(b"public chart").decode()),
        ToolExecutionContext(cwd=tmp_path, metadata={"research_store": store}),
    )
    assert allowed_result.error_code != "source_use_not_authorized"
    assert "vision model is not configured" in allowed_result.output

    user_evidence = Evidence(id="evidence_user", statement="user input",
                             source_id=user_source.id, collected_at=user_source.collected_at)
    user_digest, user_snapshot = await store.write_snapshot("user chart")
    user_artifact = artifact.model_copy(update={
        "id": "artifact_user_chart", "content_hash": user_digest,
        "snapshot": user_snapshot, "evidence_ids": [user_evidence.id],
        "assumption_evidence_ids": [],
    })
    async with current_database().transaction() as db:
        await db.execute(insert(s.entities["evidence_pool"]).values(
            workspace_id=store.workspace_id, session_id=store.session_id,
            record_id=user_evidence.id, position=2, status=user_evidence.status,
            payload=user_evidence.model_dump(mode="json"),
        ))
        await db.execute(insert(s.entities["artifacts"]).values(
            workspace_id=store.workspace_id, session_id=store.session_id,
            record_id=user_artifact.id, position=2, content_hash=user_digest,
            payload=user_artifact.model_dump(mode="json"),
        ))
    user_view, user_approved = await request_policy_view(
        store.workspace_id, store.session_id, [], {user_digest}
    )
    assert image_use_allowed(user_view, user_digest, user_approved)


async def test_user_source_can_be_read_by_model(tmp_path):
    store = ResearchStore(tmp_path, "3" * 12)
    source = await store.capture(origin_id="user-input", kind="user", content="user data")
    result = await store.apply(
        {"action": "read", "ids": [source.id], "include_content": True}, model_access=True
    )
    assert result["records"][0]["content"] == "user data"


async def test_read_rejects_cross_type_duplicate_id(tmp_path):
    store = ResearchStore(tmp_path, "4" * 12)
    source = await store.capture(origin_id="collision", kind="user", content="input")
    evidence = Evidence(id=source.id, statement="same ID", source_id=source.id,
                        collected_at=now())
    async with current_database().transaction() as db:
        await db.execute(insert(s.entities["evidence_pool"]).values(
            workspace_id=store.workspace_id, session_id=store.session_id,
            record_id=evidence.id, position=0, status=evidence.status,
            payload=evidence.model_dump(mode="json"),
        ))
    with pytest.raises(ResearchError, match="多个类型中重复"):
        await store.apply({"action": "read", "ids": [source.id]}, model_access=True)


async def test_model_read_rejects_missing_provenance(tmp_path):
    store = ResearchStore(tmp_path, "5" * 12)
    await store.load()
    evidence = Evidence(id="orphan_evidence", statement="orphan", source_id="missing_source",
                        collected_at=now())
    async with current_database().transaction() as db:
        await db.execute(insert(s.entities["evidence_pool"]).values(
            workspace_id=store.workspace_id, session_id=store.session_id,
            record_id=evidence.id, position=0, status=evidence.status,
            payload=evidence.model_dump(mode="json"),
        ))
    with pytest.raises(ResearchError, match="引用缺失"):
        await store.apply({"action": "read", "ids": [evidence.id]}, model_access=True)


async def test_model_read_rejects_wrong_provenance_type(tmp_path):
    store = ResearchStore(tmp_path, "6" * 12)
    await store.load()
    evidence = Evidence(id="wrong_type_evidence", statement="wrong type",
                        source_id="plan_as_source", collected_at=now())
    async with current_database().transaction() as db:
        await db.execute(insert(s.entities["plans"]).values(
            workspace_id=store.workspace_id, session_id=store.session_id,
            record_id="plan_as_source", position=0,
            payload={"id": "plan_as_source", "title": "Plan"},
        ))
        await db.execute(insert(s.entities["evidence_pool"]).values(
            workspace_id=store.workspace_id, session_id=store.session_id,
            record_id=evidence.id, position=0, status=evidence.status,
            payload=evidence.model_dump(mode="json"),
        ))
    with pytest.raises(ResearchError, match="类型不匹配"):
        await store.apply({"action": "read", "ids": [evidence.id]}, model_access=True)
