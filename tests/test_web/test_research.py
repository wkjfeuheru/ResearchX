"""Transport, restoration, and cancellation coverage for research memory."""

from tests.test_web.sse_client import sse_connect

import json

from researchx.state.store import ResearchStore
from tests.test_web.research_model import ResearchModel
from tests.test_web.test_app import ORIGIN, add_model, add_session, collect, submit


async def test_research_progress_provenance_and_history_restore(workspace, monkeypatch):
    client, app, _, _, cwd = workspace
    (cwd / "report.txt").write_text("报告：营业收入同比增长。", encoding="utf-8")
    sid = add_session(client, add_model(client))
    from researchx.state.models import SourceUsage
    await ResearchStore(cwd, sid).capture(
        origin_id="public-test-report", kind="file", content=(cwd / "report.txt").read_text(),
        locator=str(cwd / "report.txt"), usage=SourceUsage(category="public",
            licence_status="pending", model_use_allowed=True, internal_export_allowed=True,
            completeness="full"),
    )
    models = []
    extracted = []

    def factory(settings):
        model = ResearchModel(cwd, sid)
        models.append(model)
        return model

    monkeypatch.setattr("researchx.runtime._resolve_api_client_from_settings", factory)
    with sse_connect(client, sid, headers=ORIGIN) as socket:
        socket.receive_json()
        submit(socket, "研究公司 A")
        events = collect(socket)
    assert not events[-1]["failed"]
    assert not extracted
    operations = [
        event["message"]
        for event in events
        if event["type"] == "message" and event["message"]["role"] == "activity"
    ]
    assert operations and all("tool_input" not in row and not row["text"] for row in operations)
    progress = [event["progress"] for event in events if event["type"] == "research_progress"]
    assert any(item["total"] == 2 and item["completed"] == 0 for item in progress)
    assert [item["revision"] for item in progress] == sorted(
        {item["revision"] for item in progress}
    )
    first_completed = next(
        item for item in progress if item["tasks"] and item["tasks"][0]["status"] == "completed"
    )
    assert first_completed["tasks"][1]["status"] == "pending"
    second_started = next(
        item
        for item in progress
        if len(item["tasks"]) > 1 and item["current_task_id"] == item["tasks"][1]["id"]
    )
    assert second_started["tasks"][1]["status"] == "in_progress"
    assert second_started["tasks"][1]["started_at"]
    assert progress[-1]["completed"] == 2
    response = client.get(f"/api/sessions/{sid}").json()
    text = response["messages"][-1]["text"]
    assert "已核对原文" in text
    assert "[1]" not in text and "来源：" not in text and "report.txt" not in text
    assert "research_memory" not in json.dumps(response, ensure_ascii=False)
    store = ResearchStore(cwd, sid)
    memory = (await store.load())
    assert len(memory.task_context) == 1
    assert len(memory.reasoning_chain) == len(memory.evidence_pool) == 2
    assert len(memory.conclusions) == 1
    assert list(memory.evidence_pool.values())[-1].status == "source_checked"
    assert next(iter(memory.conclusions.values())).status == "tentative"
    assert memory.answers
    assert response["research_progress"]["completed"] == 2
    contexts = [
        m.runtime_context
        for request in models[0].requests
        for m in request.messages
        if m.runtime_context
    ]
    assert any("<research_memory>" in text for text in contexts)
    assert "# 投研工作记忆协议" in models[0].requests[0].system_prompt
    assert "# Delegation And Subagents" not in models[0].requests[0].system_prompt
    tools = {tool["name"] for tool in models[0].requests[0].tools}
    assert "research_memory" in tools and "lsp" not in tools and "enter_worktree" not in tools
    other = add_session(client, response["profile_id"])
    assert not (await ResearchStore(cwd, other).load()).evidence_pool
    with sse_connect(client, sid, headers=ORIGIN) as socket:
        ready = socket.receive_json()
        assert ready["session"]["research_progress"]["completed"] == 2
        assert ready["session"]["messages"][-1]["text"] == text


async def test_steering_cancels_old_run_then_commits_new_plan(workspace, monkeypatch):
    client, app, _, _, cwd = workspace
    (cwd / "report.txt").write_text("报告：营业收入同比增长。", encoding="utf-8")
    sid = add_session(client, add_model(client))
    from researchx.state.models import SourceUsage
    await ResearchStore(cwd, sid).capture(
        origin_id="public-test-report", kind="file", content=(cwd / "report.txt").read_text(),
        locator=str(cwd / "report.txt"), usage=SourceUsage(category="public",
            licence_status="pending", model_use_allowed=True, internal_export_allowed=True,
            completeness="full"),
    )
    models = []

    def factory(settings):
        model = ResearchModel(
            cwd, sid, slow=not models, title="公司 A 研究" if not models else "公司 B 研究"
        )
        models.append(model)
        return model

    monkeypatch.setattr("researchx.runtime._resolve_api_client_from_settings", factory)
    with sse_connect(client, sid, headers=ORIGIN) as socket:
        socket.receive_json()
        submit(socket, "研究公司 A", request_id="old")
        collect(socket, "delta")
        socket.send_json(
            {
                "type": "steer",
                "request_id": "new",
                "target_request_id": "old",
                "text": "改研究公司 B",
            }
        )
        events = []
        while True:
            item = socket.receive_json()
            events.append(item)
            if item["type"] == "done" and item["request_id"] == "new":
                break
    assert (
        next(event for event in events if event["type"] == "steer_accepted")["next_request_id"]
        == "new"
    )
    assert not events[-1]["failed"]
    memory = (await ResearchStore(cwd, sid).load())
    assert len(memory.plans) == 2
    old, new = list(memory.plans.values())
    assert old.archived and all(task.status == "cancelled" for task in old.tasks)
    assert not new.archived and new.title == "公司 B 研究"
    assert memory.pending_steers["new"]["text"] == "改研究公司 B"
    assert not memory.research_state.replan_required
    assert events[-1]["session"]["research_progress"]["completed"] == 2
    new_text = json.dumps(
        [event for event in events if event["request_id"] == "new"], ensure_ascii=False
    )
    assert "report.txt" in new_text
    assert not app.state.workspace.lock.locked()
    rows = events[-1]["session"]["messages"]
    modification = next(index for index, row in enumerate(rows) if row["id"] == "new")
    assert all("旧研究执行中" not in row["text"] for row in rows[modification + 1 :])
    assert rows[modification]["turn_id"] == "new"
    old_partial = next(row for row in rows if "旧研究执行中" in row["text"])
    assert old_partial["turn_id"] == "old" and old_partial["turn_status"] == "stopped"
    assert old_partial["phase"] == "progress"
    assert rows[-1]["turn_id"] == "new" and rows[-1]["phase"] == "final"


async def test_old_web_history_does_not_become_verified_memory(workspace):
    client, app, _, _, cwd = workspace
    sid = add_session(client, add_model(client))
    record = (await app.state.workspace.store.load_by_id(cwd, sid))
    record["messages"] = [
        {"role": "assistant", "content": [{"type": "text", "text": "旧摘要：研究已完成"}]}
    ]
    record["display_messages"] = [
        {"id": "old-tool", "role": "tool_result", "text": "内部工具详情"},
        {"id": "old-reply", "role": "assistant", "text": "旧摘要：研究已完成"},
    ]
    (await app.state.workspace.store.write(record))
    response = client.get(f"/api/sessions/{sid}").json()
    assert len(response["messages"]) == 1
    assert response["messages"][0]["text"] == "旧摘要：研究已完成"
    assert not (await ResearchStore(cwd, sid).load()).conclusions


async def test_corrupt_memory_returns_explicit_error_and_preserves_records(workspace):
    client, _, _, _, cwd = workspace
    sid = add_session(client, add_model(client))
    store = ResearchStore(cwd, sid)
    (await store.capture(origin_id="source", content="report"))
    from tests.postgres_helpers import raw_state, corrupt_state
    damaged = await raw_state(store)
    damaged["research_state"]["current_plan_id"] = "missing"
    await corrupt_state(store, damaged)
    response = client.get(f"/api/sessions/{sid}")
    assert response.status_code == 409
    assert "损坏" in response.json()["detail"]
    assert await raw_state(store) == damaged


async def test_crash_after_steer_acceptance_restores_requirement_without_execution(workspace):
    client, _, _, _, cwd = workspace
    sid = add_session(client, add_model(client))
    store = ResearchStore(cwd, sid)
    (await store.interrupt(request_id="accepted-change", target_request_id="old", text="只研究公司 B"))
    response = client.get(f"/api/sessions/{sid}").json()
    assert response["research_progress"]["replan_required"]
    assert response["messages"][-1] == {
        "id": "accepted-change",
        "role": "user",
        "text": "只研究公司 B",
    }
    assert not (await store.load()).plans
    again = client.get(f"/api/sessions/{sid}").json()
    assert again["messages"] == response["messages"]
