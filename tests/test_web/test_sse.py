"""SSE ownership, bounded delivery, command idempotency and disconnect persistence."""

import asyncio
import json

import httpx
import pytest

from researchx.web.events import EventChannel, encode_event
from researchx.web.app import create_app
from researchx.web.runtime import SessionController
from tests.test_web.sse_client import sse_connect
from tests.test_web.test_app import add_model, add_session, collect, submit


def test_headers_unicode_order_and_ready_snapshot(workspace):
    client, _, _, _, _ = workspace
    sid = add_session(client, add_model(client))
    with sse_connect(client, sid) as stream:
        assert stream.response_headers["content-type"] == "text/event-stream; charset=utf-8"
        assert stream.response_headers["cache-control"] == "no-cache, no-transform"
        assert stream.response_headers["x-accel-buffering"] == "no"
        assert stream.response_headers["x-content-type-options"] == "nosniff"
        ready = stream.receive_json()
        assert ready["type"] == "ready" and ready["session"]["session_id"] == sid
        submit(stream, "中文与换行\n下一行")
        events = collect(stream)
        types = [event["type"] for event in events]
        assert types[0] == "started" and types[-1] == "done"
        assert types.index("delta") < types.index("message") < types.index("done")
        assert all(event["request_id"] == "r1" and event["session_id"] == sid for event in events)
        assert "中文" in "".join(event["text"] for event in events if event["type"] == "delta")
    with sse_connect(client, sid) as restored:
        assert len(restored.receive_json()["session"]["messages"]) == 2


def test_reconnect_sends_active_request_before_replay(workspace):
    client, app, _, _, _ = workspace
    sid = add_session(client, add_model(client))
    controller = SessionController(sid, app.state.workspace)
    controller.request_id = "ongoing"
    controller.accepting = True
    controller.event_seq = 1
    controller.event_log = [{
        "type": "delta", "session_id": sid, "request_id": "ongoing",
        "event_id": 1, "id": "draft", "text": "partial", "turn_id": "ongoing",
    }]
    app.state.workspace.active_runs[sid] = controller
    with sse_connect(client, sid) as stream:
        ready = stream.receive_json()
        replay = stream.receive_json()
        assert ready["type"] == "ready" and ready["request_id"] == "ongoing"
        assert ready["running"] is True
        assert replay["type"] == "delta" and replay["event_id"] == 1


async def test_bounded_channel_backpressure_order_detach_and_heartbeat():
    channel = EventChannel(capacity=1, heartbeat_seconds=0.01)
    first = {"type": "message", "text": "中文\n下一行"}
    await channel.publish(first)
    first["text"] = "mutated"
    await channel.publish({"type": "done"})
    assert channel.detached.is_set() and channel.queue.qsize() == 1
    frames = channel.stream()
    frame = await anext(frames)
    assert frame.endswith("\n\n") and "中文" in frame
    assert json.loads(frame[6:])["text"] == "中文\n下一行"
    with pytest.raises(StopAsyncIteration):
        await anext(frames)
    channel = EventChannel(capacity=2, heartbeat_seconds=0.01)
    frames = channel.stream()
    assert await anext(frames) == ": keep-alive\n\n"
    await frames.aclose()
    await channel.publish({"type": "prompt"})
    channel.detach()
    await channel.publish({"type": "error"})
    assert channel.queue.qsize() == 1
    assert encode_event({"text": "你好"}) == 'data: {"text":"你好"}\n\n'
    assert encode_event({"event_id": 7, "text": "你好"}) == 'id: 7\ndata: {"event_id":7,"text":"你好"}\n\n'


def test_command_ownership_validation_duplicate_and_restart(workspace):
    client, app, requests, _, cwd = workspace
    sid = add_session(client, add_model(client))
    endpoint = f"/api/sessions/{sid}/commands"
    payload = {"type": "submit", "request_id": "stable", "text": "hello"}
    accepted = client.post(endpoint, json=payload)
    assert accepted.status_code == 202 and accepted.json()["accepted"] is True
    for _ in range(100):
        messages = client.get(f"/api/sessions/{sid}").json()["messages"]
        if any(row.get("role") == "assistant" for row in messages):
            break
    else:
        raise AssertionError("headless submit did not run")
    assert client.post("/api/sessions/missing/commands", json=payload).status_code == 404
    with sse_connect(client, sid) as stream:
        stream.receive_json()
        ambiguous = {"type": "submit", "request_id": "ambiguous", "text": "点评新闻和财报"}
        rejected = stream.command(ambiguous)
        assert rejected.status_code == 422
        assert "发起人选择" in rejected.json()["detail"]
        accepted = stream.command({**ambiguous, "research_type": "financial_commentary"})
        assert accepted.status_code == 202
        collect(stream)
        duplicate = client.post(endpoint, json=payload)
        assert duplicate.status_code == 202 and duplicate.json()["duplicate"] is True
        assert (
            stream.command({"type": "submit", "request_id": "bad", "text": ""}).status_code == 422
        )
        assert stream.command({"type": "unsupported", "request_id": "bad"}).status_code == 422
        assert stream.command(payload).status_code == 202
        assert stream.command(payload).json()["duplicate"] is True
        assert len(requests) == 2
        assert stream.command({**payload, "text": "different"}).status_code == 409
        with pytest.raises(httpx.HTTPStatusError) as duplicate:
            with sse_connect(client, sid):
                pass
        assert duplicate.value.response.status_code == 409
    assert not app.state.workspace.connections
    from fastapi.testclient import TestClient

    with TestClient(create_app(str(cwd)), base_url="http://localhost") as restarted:
        with sse_connect(restarted, sid) as stream:
            stream.receive_json()
            assert stream.command(payload).json()["duplicate"] is True
            assert len(requests) == 2


def test_simultaneous_duplicate_commands_claim_one_execution(workspace):
    client, app, requests, _, _ = workspace
    sid = add_session(client, add_model(client))
    with sse_connect(client, sid) as stream:
        stream.receive_json()

        async def submit_pair():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as commands:
                return await asyncio.gather(
                    *[
                        commands.post(
                            f"/api/sessions/{sid}/commands",
                            headers={"X-ResearchX-Connection": stream.connection_id},
                            json={"type": "submit", "request_id": "same", "text": "slow"},
                        )
                        for _ in range(2)
                    ]
                )

        responses = client.portal.call(submit_pair)
        assert [response.status_code for response in responses] == [202, 202]
        assert sum(response.json().get("duplicate", False) for response in responses) == 1
        collect(stream, "delta")
        assert len(requests) == 1
        stream.send_json({"type": "cancel", "request_id": "same"})
        assert collect(stream)[-1]["cancelled"]


def test_stale_prompt_parallel_submit_and_disconnect(workspace):
    client, app, _, clients, _ = workspace
    sid = add_session(client, add_model(client))
    with sse_connect(client, sid) as stream:
        stream.receive_json()
        submit(stream, "ask")
        prompt = collect(stream, "prompt")[-1]
        response = {"type": "response", "request_id": "r1", "prompt_id": "stale", "answer": "allow"}
        assert stream.command(response).status_code == 409
        assert not app.state.workspace.connections[sid].prompts[prompt["prompt_id"]].done()
        assert (
            stream.command({"type": "submit", "request_id": "other", "text": "hello"}).status_code
            == 409
        )
        assert stream.command({"type": "cancel", "request_id": "stale"}).status_code == 409
    assert not app.state.workspace.connections and not app.state.workspace.lock.locked()
    live = app.state.workspace.active_runs.get(sid)
    assert live is not None and live.busy
    assert not all(client.closed for client in clients)


def test_disconnect_during_steer_never_starts_orphan(workspace, monkeypatch):
    client, app, requests, _, _ = workspace
    sid = add_session(client, add_model(client))
    cancellation_started = asyncio.Event()
    release = asyncio.Event()
    original_cancel = SessionController.cancel

    async def delayed_cancel(self):
        cancellation_started.set()
        await release.wait()
        await original_cancel(self)

    monkeypatch.setattr(SessionController, "cancel", delayed_cancel)
    with sse_connect(client, sid) as stream:
        stream.receive_json()
        submit(stream, "slow", request_id="old")
        collect(stream, "delta")
        payload = {
            "type": "steer",
            "request_id": "new",
            "target_request_id": "old",
            "text": "新目标",
        }
        assert stream.command(payload).status_code == 202
        assert stream.command(payload).json()["duplicate"] is True
        collect(stream, "steer_accepted")
        client.portal.call(cancellation_started.wait)
        # Disconnect removes the subscription. Releasing cancellation still starts one successor.
        client.portal.call(stream.closed.set)
        client.portal.call(release.set)
        for _ in range(50):
            saved = client.get(f"/api/sessions/{sid}").json()["messages"]
            if len(requests) >= 2 and not any(
                row.get("turn_status") == "running" for row in saved
            ):
                break
            client.portal.call(asyncio.sleep, 0.05)
        else:
            raise AssertionError("steer during disconnect did not settle one successor")
    assert len(requests) == 2
    assert not app.state.workspace.connections
    assert not app.state.workspace.lock.locked()
    saved = client.get(f"/api/sessions/{sid}").json()["messages"]
    assert any(row["text"] == "部分回复" for row in saved)
    assert not any(row.get("turn_status") == "running" for row in saved)


async def test_asgi_send_failure_always_detaches_response():
    from researchx.web.events import SessionEventResponse
    from starlette.requests import ClientDisconnect

    channel = EventChannel(capacity=1)
    await channel.publish({"type": "ready"})
    closed = []

    async def close():
        channel.detach()
        closed.append(True)

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        raise OSError("broken pipe")

    response = SessionEventResponse(channel.stream(), close)
    with pytest.raises(ClientDisconnect):
        await response({"asgi": {"spec_version": "2.4"}, "type": "http"}, receive, send)
    assert closed == [True] and channel.detached.is_set()


def test_accepted_steer_storage_error_stops_old_run_and_reports_failure(workspace, monkeypatch):
    client, app, requests, _, _ = workspace
    sid = add_session(client, add_model(client))
    from researchx.state.store import ResearchStore

    async def fail_replan(self, request_id):
        raise OSError("simulated replan write failure")

    with sse_connect(client, sid) as stream:
        stream.receive_json()
        submit(stream, "slow", request_id="old")
        collect(stream, "delta")
        monkeypatch.setattr(ResearchStore, "require_replan", fail_replan)
        assert (
            stream.command(
                {
                    "type": "steer",
                    "request_id": "new",
                    "target_request_id": "old",
                    "text": "新目标",
                }
            ).status_code
            == 202
        )
        events = []
        while True:
            event = stream.receive_json()
            events.append(event)
            if event["type"] == "done" and event["request_id"] == "new":
                break
        assert events[-1]["failed"]
        assert any(event["type"] == "error" for event in events)
        assert len(requests) == 1 and app.state.workspace.execution_owner is None
        assert not app.state.workspace.lock.locked()
        assert not any(
            row.get("turn_status") == "running" for row in events[-1]["session"]["messages"]
        )


async def test_failed_control_still_releases_owner_on_close(tmp_path):
    from researchx.web.workspace import WebWorkspace

    workspace = WebWorkspace(str(tmp_path))
    controller = SessionController("a" * 12, workspace)
    workspace.execution_owner = controller.connection_id
    controller.stopping = True

    async def failed_control():
        await asyncio.sleep(0)
        raise RuntimeError("control failed")

    controller.control_task = asyncio.create_task(failed_control())
    with pytest.raises(RuntimeError, match="control failed"):
        await controller.close()
    assert workspace.execution_owner is None and controller.closed
