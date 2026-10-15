"""Real PostgreSQL and process-exit coverage for accepted requests and tool receipts."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from researchx.config.settings import PermissionSettings
from researchx.engine.messages import ConversationMessage, ToolUseBlock
from researchx.engine.query import QueryContext, run_query
from researchx.permissions.checker import PermissionChecker
from researchx.storage.turns import TurnJournal, CheckpointError, RequestConflict
from researchx.web.models import CommandRequest
from researchx.web.runtime import SessionController
from researchx.web.workspace import WebWorkspace
from researchx.tools.base import ToolRegistry


CHILD = r'''
import asyncio, os, sys
from pathlib import Path
from unittest.mock import AsyncMock
from researchx.config.settings import PermissionSettings
from researchx.permissions.checker import PermissionChecker
from researchx.storage.database import Database, bind_database
from researchx.storage.turns import TurnJournal
from researchx.engine.messages import ConversationMessage, ToolUseBlock
from researchx.engine.query import QueryContext
from researchx.services.execution.tool_execution import ToolExecutionService
from researchx.web.models import CommandRequest
from researchx.tools.base import ToolRegistry
from tests.test_harness.test_execution import Write
async def main():
    cwd, sid, boundary = sys.argv[1:]
    database = Database(os.environ['RESEARCHX_TEST_DATABASE_URL'])
    with bind_database(database):
        journal = TurnJournal(cwd, sid, 'stable')
        command = CommandRequest(type='submit', request_id='stable', text='run')
        accepted, fresh = await journal.accept(command.model_dump(mode='json'), {'user_text':'run', 'profile_id':'test', 'settings':{}, 'attachments':[]})
        assert fresh
        await journal.save(phase='accepted', messages=[], projection={'usage':{}})
        if boundary == 'accepted':
            os._exit(23)
        message = ConversationMessage.from_user_text('run').model_copy(update={'message_id':accepted['message_id']})
        calls = [ToolUseBlock(id='call-1', name='write_probe', input={'value':42})]
        if boundary == 'partial_batch':
            calls.append(ToolUseBlock(id='call-2', name='write_probe', input={'value':99}))
        messages = [message, ConversationMessage(role='assistant', content=calls)]
        await journal.save(phase='model_response', messages=messages, projection={'usage':{'input_tokens':7,'output_tokens':3}, 'stop_reason':'tool_use'})
        if boundary == 'model_response':
            os._exit(23)
        await journal.save(phase='before_tools', messages=messages)
        if boundary == 'before_tools':
            os._exit(23)
        tool = Write()
        registry = ToolRegistry()
        registry.register(tool)
        context = QueryContext(api_client=AsyncMock(), tool_registry=registry,
            permission_checker=PermissionChecker(PermissionSettings(mode='full_auto')),
            cwd=Path(cwd), model='test', system_prompt='test', max_tokens=100,
            execution_session_id=sid, tool_metadata={'session_id':sid, 'checkpoint_writer':journal})
        result = await ToolExecutionService().execute(context, tool.name, 'call-1', {'value':42})
        assert not result.is_error, result.content
        os._exit(23)
asyncio.run(main())
'''


@pytest.mark.parametrize("boundary", ["accepted", "model_response", "before_tools", "tool_completed", "partial_batch"])
async def test_process_exit_restores_history_and_receipts_without_rescheduling(tmp_path, boundary):
    workspace = WebWorkspace(str(tmp_path))
    record = await workspace.store.create("test")
    sid = record["session_id"]
    result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", CHILD, str(tmp_path), sid, boundary],
        cwd=Path(__file__).resolve().parents[2], env=os.environ.copy(), capture_output=True, text=True, timeout=30)
    assert result.returncode == 23, result.stderr
    recovered = await workspace.record(sid)
    assert any(m.get("message_id") for m in recovered["messages"])
    assert (await TurnJournal(str(tmp_path), sid, "stable").get())["status"] == "interrupted"
    controller = SessionController(sid, workspace)
    controller.ready = True
    ack = await controller.command(CommandRequest(type="submit", request_id="stable", text="run"))
    assert ack["duplicate"] is True and controller.task is None
    if boundary == "accepted":
        assert not (tmp_path / "side_effect").exists()
    else:
        raw = [ConversationMessage.model_validate(m) for m in recovered["messages"]]
        results = [b for m in raw for b in m.content if b.type == "tool_result"]
        assert results
        assert recovered["usage"]["input_tokens"] == 7 and recovered["usage"]["output_tokens"] == 3
        if boundary in {"model_response", "before_tools"}:
            assert results[0].is_error and not (tmp_path / "side_effect").exists()
        else:
            assert results[0].content == "committed"
            assert results[0].result_metadata["replayed_receipt"]
            assert (tmp_path / "side_effect").read_text() == "42"
        if boundary == "partial_batch":
            assert [r.tool_use_id for r in results] == ["call-1", "call-2"]
            assert results[1].is_error


async def test_different_payloads_conflict_and_accept_transaction_rolls_back(tmp_path):
    journal = TurnJournal(str(tmp_path), "session", "request")
    command = CommandRequest(type="submit", request_id="request", text="run").model_dump(mode="json")
    await journal.accept(command, {"user_text":"run"})
    for changed in [{"text":"new"}, {"type":"steer"}, {"profile_id":"other"}, {"attachment_ids":["different"]}]:
        with pytest.raises(RequestConflict):
            await journal.accept({**command, **changed}, {"user_text":"new"})
    other = TurnJournal(str(tmp_path), "session", "rolled-back")
    with pytest.raises(RuntimeError):
        async with journal.database.transaction():
            await other.accept({**command, "request_id":"rolled-back"}, {"user_text":"run"})
            raise RuntimeError("failed interrupt")
    assert await other.get() is None


async def test_checkpoint_failure_prevents_tools_and_next_model_call(tmp_path):
    from researchx.api.client import ApiMessageCompleteEvent
    from researchx.api.usage import UsageSnapshot
    from tests.test_harness.test_execution import Write

    tool = Write()
    registry = ToolRegistry()
    registry.register(tool)
    model_calls = []

    class Client:
        async def stream_message(self, request):
            model_calls.append(request)
            yield ApiMessageCompleteEvent(message=ConversationMessage(role="assistant", content=[ToolUseBlock(id="call", name=tool.name, input={"value":1})]), usage=UsageSnapshot(), stop_reason=None)

    class Writer:
        async def save(self, *, phase, **kwargs):
            if phase == "before_tools":
                raise CheckpointError("database unavailable")

    context = QueryContext(api_client=Client(), tool_registry=registry, permission_checker=PermissionChecker(PermissionSettings(mode="full_auto")),
        cwd=tmp_path, model="test", system_prompt="test", max_tokens=100, tool_metadata={"checkpoint_writer":Writer()}, research_memory_enabled=False, context_window_tokens=200000)
    with pytest.raises(CheckpointError):
        async for _ in run_query(context, [ConversationMessage.from_user_text("run")]):
            pass
    assert len(model_calls) == 1 and tool.calls == 0


async def test_recovery_does_not_mix_same_call_id_from_child_scope(tmp_path):
    from tests.test_harness.test_execution import Write
    from researchx.services.execution.tool_execution import ToolExecutionService
    from researchx.services.execution.operations import operation_identity

    workspace = WebWorkspace(str(tmp_path))
    record = await workspace.store.create("test")
    sid = record["session_id"]
    journal = TurnJournal(str(tmp_path), sid, "request")
    command = CommandRequest(type="submit", request_id="request", text="run")
    accepted, _ = await journal.accept(command.model_dump(mode="json"), {"user_text":"run"})
    user = ConversationMessage.from_user_text("run").model_copy(update={"message_id":accepted["message_id"]})
    child = tmp_path / "child"
    child.mkdir()
    for cwd, value in [(tmp_path, 42), (child, 99)]:
        tool = Write()
        registry = ToolRegistry()
        registry.register(tool)
        messages = [user, ConversationMessage(role="assistant", content=[ToolUseBlock(id="same", name=tool.name, input={"value":value})])]
        await journal.save(phase="before_tools", messages=messages, scope=str(cwd))
        context = QueryContext(api_client=None, tool_registry=registry,
            permission_checker=PermissionChecker(PermissionSettings(mode="full_auto")), cwd=cwd,
            execution_workspace=tmp_path, execution_session_id=sid, model="test", system_prompt="test", max_tokens=100,
            tool_metadata={"session_id":sid, "checkpoint_writer":journal})
        result = await ToolExecutionService().execute(context, tool.name, "same", {"value":value})
        assert not result.is_error
    recovered = await workspace.record(sid)
    result = next(b for m in recovered["messages"] for b in m["content"] if b["type"] == "tool_result")
    assert result["result_metadata"]["operation_id"] == operation_identity(journal.workspace, sid, str(tmp_path), "same")
