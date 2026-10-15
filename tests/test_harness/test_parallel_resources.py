"""Resource admission, not is_read_only heuristics, controls actual overlap."""

import asyncio
from typing import Literal

import pytest
from pydantic import BaseModel

from researchx.config.settings import PermissionSettings
from researchx.engine.query import QueryContext, _execute_tool_call
from researchx.permissions.checker import PermissionChecker
from researchx.tools.base import BaseTool, ToolRegistry, ToolResult
from researchx.security.network_guard import network_request_limit, fetch_public_http_response
from researchx.services.execution.operations import _conflicts


class ProbeInput(BaseModel):
    path: str
    mode: Literal["read", "write"]
    label: str


class ResourceProbe(BaseTool[ProbeInput]):
    name = "resource_probe"
    description = "test"
    input_model = ProbeInput

    def __init__(self):
        self.entered = {key: asyncio.Event() for key in ["a", "b"]}
        self.release = asyncio.Event()
        self.both = asyncio.Event()
        self.active = self.peak = 0

    def is_read_only(self, arguments):
        return arguments.mode == "read"

    def execution_contract(self, arguments=None):
        return {
            "name": self.name,
            "effect": "read_only" if arguments and arguments.mode == "read" else "local_write",
            "resources_read": ("path",) if arguments and arguments.mode == "read" else (),
            "resources_write": () if arguments and arguments.mode == "read" else ("path",),
            "parallelism": "resources",
        }

    async def execute(self, arguments, context):
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.entered[arguments.label].set()
        if self.active == 2:
            self.both.set()
        try:
            await self.release.wait()
            return ToolResult(output="done")
        finally:
            self.active -= 1


def setup(tmp_path):
    probe = ResourceProbe()
    registry = ToolRegistry()
    registry.register(probe)
    context = QueryContext(
        api_client=None,
        tool_registry=registry,
        permission_checker=PermissionChecker(PermissionSettings(allowed_tools=[probe.name])),
        cwd=tmp_path,
        model="fixture",
        system_prompt="",
        max_tokens=64,
    )
    return context, probe


@pytest.mark.parametrize(
    "modes,paths",
    [
        (["read", "read"], ["same", "same"]),
        (["write", "write"], ["one", "two"]),
        (["read", "write"], ["one", "two"]),
    ],
)
async def test_nonconflicting_operations_really_overlap(tmp_path, modes, paths):
    context, probe = setup(tmp_path)
    tasks = [
        asyncio.create_task(
            _execute_tool_call(
                context, probe.name, label, {"path": path, "mode": mode, "label": label}
            )
        )
        for label, mode, path in zip(["a", "b"], modes, paths)
    ]
    try:
        await asyncio.wait_for(probe.both.wait(), 5)
        assert probe.peak == 2
    finally:
        probe.release.set()
        results = await asyncio.gather(*tasks)
    assert all(not result.is_error for result in results)


@pytest.mark.parametrize(
    "modes,paths",
    [
        (["write", "write"], ["same", "same"]),
        (["read", "write"], ["same", "same"]),
        (["write", "read"], ["directory", "directory/child"]),
    ],
)
async def test_conflicting_operations_wait_for_release(tmp_path, modes, paths):
    context, probe = setup(tmp_path)
    first = asyncio.create_task(
        _execute_tool_call(
            context, probe.name, "a", {"path": paths[0], "mode": modes[0], "label": "a"}
        )
    )
    await asyncio.wait_for(probe.entered["a"].wait(), 5)
    second = asyncio.create_task(
        _execute_tool_call(
            context, probe.name, "b", {"path": paths[1], "mode": modes[1], "label": "b"}
        )
    )
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(probe.entered["b"].wait(), 0.15)
    finally:
        probe.release.set()
        results = await asyncio.gather(first, second)
    assert all(not result.is_error for result in results)
    assert probe.peak == 1


async def test_nested_requests_share_one_host_owned_limit(monkeypatch):
    active = peak = 0
    first_batch = asyncio.Event()
    release = asyncio.Event()

    async def request(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 4:
            first_batch.set()
        try:
            await release.wait()
            return object()
        finally:
            active -= 1

    monkeypatch.setattr("researchx.security.network_guard._fetch_public_http_response", request)
    with network_request_limit(asyncio.Semaphore(4)):
        tasks = [
            asyncio.create_task(fetch_public_http_response("https://example.com"))
            for _ in range(12)
        ]
    try:
        await asyncio.wait_for(first_batch.wait(), 5)
        assert active == 4
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert peak == 4 and active == 0


def test_unknown_resource_excludes_reads_and_writes():
    assert _conflicts({"read": [], "write": ["*"]}, {"read": ["network.http"], "write": []})
    assert _conflicts({"read": [], "write": ["*"]}, {"read": [], "write": ["/workspace/a"]})


async def test_one_tool_batch_is_bounded_to_four_workers(tmp_path):
    from researchx.engine.query import _execute_tool_batch
    from researchx.engine.messages import ToolUseBlock

    context, probe = setup(tmp_path)
    probe.entered = {str(i): asyncio.Event() for i in range(9)}
    completed = []

    async def consume():
        async for _, index, result in _execute_tool_batch(
            context,
            [
                ToolUseBlock(
                    id=str(i),
                    name=probe.name,
                    input={"path": str(i), "mode": "read", "label": str(i)},
                )
                for i in range(9)
            ],
        ):
            if result:
                completed.append((index, result))

    task = asyncio.create_task(consume())
    try:

        async def first_four():
            while probe.active < 4:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(first_four(), 5)
        assert sum(event.is_set() for event in probe.entered.values()) == 4
    finally:
        probe.release.set()
        await task
    assert probe.peak == 4 and len(completed) == 9


async def test_workspace_resources_conflict_across_execution_scopes(tmp_path):
    from researchx.services.execution.operations import OperationStore

    ledger = OperationStore(tmp_path)
    first = await ledger.prepare(
        session="parent",
        scope="scope-a",
        run="run-parent",
        call="a",
        tool="write_file",
        version="1",
        digest="a",
        effect="local_write",
        resources={"read": [], "write": [str(tmp_path / "same")]},
    )
    second = await ledger.prepare(
        session="child",
        scope="scope-b",
        run="run-child",
        call="b",
        tool="read_file",
        version="1",
        digest="b",
        effect="read_only",
        resources={"read": [str(tmp_path / "same")], "write": []},
    )
    assert await ledger.claim(first["operation_id"], "owner")
    assert await ledger.claim(second["operation_id"], "owner") is None


async def test_closing_outer_query_stream_settles_tool_worker(tmp_path):
    from researchx.engine.query import run_query
    from researchx.engine.messages import ConversationMessage, ToolUseBlock
    from researchx.engine.stream_events import ToolExecutionStarted
    from researchx.api.usage import UsageSnapshot
    from tests.test_engine.test_query_engine import FakeApiClient, _FakeResponse

    context, probe = setup(tmp_path)
    context.max_tokens = 1024
    context.context_window_tokens = 200000
    context.api_client = FakeApiClient(
        [
            _FakeResponse(
                ConversationMessage(
                    role="assistant",
                    content=[
                        ToolUseBlock(
                            id="close-probe",
                            name=probe.name,
                            input={"path": "one", "mode": "read", "label": "a"},
                        )
                    ],
                ),
                UsageSnapshot(),
            )
        ]
    )
    stream = run_query(context, [ConversationMessage.from_user_text("test")])
    async for event, _ in stream:
        if isinstance(event, ToolExecutionStarted):
            await asyncio.wait_for(probe.entered["a"].wait(), 5)
            break
    await stream.aclose()
    assert probe.active == 0
