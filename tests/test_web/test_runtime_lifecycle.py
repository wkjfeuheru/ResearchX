"""Lifecycle failures cannot leak adopted resources or discard committed answers."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from researchx.config import Settings
from researchx.runtime import RuntimeBundle, RuntimeCleanupError, build_runtime, close_runtime, start_runtime


@pytest.mark.parametrize("owned", [True, False])
async def test_initialization_failure_closes_only_owned_clients(tmp_path, monkeypatch, owned):
    api = AsyncMock()
    manager = AsyncMock()
    monkeypatch.setattr("researchx.runtime.McpClientManager", lambda configs: manager)
    monkeypatch.setattr("researchx.runtime.create_research_tool_registry", lambda manager: (_ for _ in ()).throw(ValueError("register failed")))
    with pytest.raises(ValueError, match="register failed"):
        await build_runtime(cwd=str(tmp_path), session_id="a" * 12, api_client=api, owns_api_client=owned, settings_override=Settings())
    manager.close.assert_awaited_once()
    assert api.close.await_count == int(owned)


async def test_state_load_failure_closes_mcp_and_client(tmp_path, monkeypatch):
    api = AsyncMock()
    manager = AsyncMock()
    manager.list_tools = lambda: []
    store = AsyncMock()
    store.load.side_effect = ValueError("corrupt state")
    monkeypatch.setattr("researchx.runtime.McpClientManager", lambda configs: manager)
    with pytest.raises(ValueError, match="corrupt state"):
        await build_runtime(cwd=str(tmp_path), session_id="a" * 12, api_client=api, settings_override=Settings(), research_store_override=store)
    manager.close.assert_awaited_once()
    api.close.assert_awaited_once()


async def test_cleanup_failure_retains_initialization_error(tmp_path, monkeypatch):
    api = AsyncMock()
    api.close.side_effect = RuntimeError("close failed")
    monkeypatch.setattr("researchx.runtime.create_research_tool_registry", lambda manager: (_ for _ in ()).throw(ValueError("original")))
    with pytest.raises(ValueError, match="original") as error:
        await build_runtime(cwd=str(tmp_path), session_id="a" * 12, api_client=api, settings_override=Settings(), connect_mcp=False)
    assert len(error.value.cleanup_errors) == 1


async def test_close_is_unique_waits_after_cancellation_and_closes_remaining_resources(tmp_path, monkeypatch):
    api = AsyncMock()
    manager = AsyncMock()
    manager.close.side_effect = RuntimeError("MCP close failed")
    hook = AsyncMock()
    started = asyncio.Event()
    release = asyncio.Event()

    async def stop(runtime_id):
        started.set()
        await release.wait()

    monkeypatch.setattr("researchx.sandbox.session.stop_runtime_sandboxes", stop)
    bundle = RuntimeBundle(api_client=api, cwd=str(tmp_path), mcp_manager=manager, tool_registry=None, hook_executor=hook, engine=SimpleNamespace(), session_id="a" * 12)
    await start_runtime(bundle)
    first = asyncio.create_task(close_runtime(bundle))
    await started.wait()
    first.cancel()
    await asyncio.sleep(0)
    assert not first.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    with pytest.raises(RuntimeCleanupError):
        await close_runtime(bundle)
    assert bundle.close_task.done()
    assert hook.execute.await_count == 2
    manager.close.assert_awaited_once()
    api.close.assert_awaited_once()


def test_final_snapshot_failure_still_closes_runtime_and_releases_owner(workspace, monkeypatch):
    from tests.test_web.test_app import add_model, add_session, collect, submit
    from tests.test_web.sse_client import sse_connect

    client, app, requests, providers, _ = workspace
    sid = add_session(client, add_model(client))

    async def fail_snapshot(**kwargs):
        raise OSError("snapshot unavailable")

    monkeypatch.setattr(app.state.workspace.store, "save_snapshot", fail_snapshot)
    with sse_connect(client, sid) as stream:
        stream.receive_json()
        submit(stream, "hello")
        events = collect(stream)
        assert events[-1]["failed"]
    assert requests and all(provider.closed for provider in providers)
    assert app.state.workspace.execution_owner is None
    assert not app.state.workspace.lock.locked()
    assert client.get(f"/api/sessions/{sid}").status_code == 200


def test_committed_answer_survives_cleanup_failure_and_diagnostic_is_persisted(workspace, monkeypatch):
    from tests.test_web.test_app import add_model, add_session, collect, submit
    from tests.test_web.sse_client import sse_connect
    import researchx.web.runtime as runtime

    original_close = runtime.close_runtime

    async def failed_close(bundle):
        await original_close(bundle)
        raise RuntimeError("simulated cleanup failure")

    client, app, _, _, _ = workspace
    sid = add_session(client, add_model(client))
    monkeypatch.setattr(runtime, "close_runtime", failed_close)
    with sse_connect(client, sid) as stream:
        stream.receive_json()
        submit(stream, "hello")
        events = collect(stream)
        assert not events[-1]["failed"] and events[-1]["cleanup_failed"]
    saved = client.get(f"/api/sessions/{sid}").json()["messages"]
    assert any(row.get("phase") == "final" and "hello" in row["text"] for row in saved)
    assert any(row.get("label") == "运行资源清理" and row.get("status") == "failed" for row in saved)
    assert app.state.workspace.execution_owner is None


@pytest.mark.parametrize("owned", [True, False])
async def test_injected_client_ownership_starts_before_settings_load(tmp_path, monkeypatch, owned):
    api = AsyncMock()
    monkeypatch.setattr("researchx.runtime.load_settings", lambda: (_ for _ in ()).throw(ValueError("settings failed")))
    with pytest.raises(ValueError, match="settings failed"):
        await build_runtime(cwd=str(tmp_path), api_client=api, owns_api_client=owned)
    assert api.close.await_count == int(owned)
