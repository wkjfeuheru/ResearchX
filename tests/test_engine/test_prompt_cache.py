"""Regression contracts for append-only request context and cache accounting."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import httpx
import json
import pytest


from researchx.api.client import ApiMessageCompleteEvent, ApiMessageRequest
from researchx.api.codex_client import _convert_messages_to_codex
from researchx.api.openai_client import OpenAICompatibleClient, _convert_messages_to_openai
from researchx.api.usage import UsageSnapshot, usage_from_provider
from researchx.config.settings import Settings, PermissionSettings
from researchx.engine.cost_tracker import CostTracker
from researchx.engine.messages import (
    ConversationMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
)
from researchx.engine.query_engine import QueryEngine
from researchx.permissions.checker import PermissionChecker
from researchx.prompts.context import RuntimePrompt, build_runtime_prompt
from researchx.prompts.environment import get_environment_info
from researchx.services.compact import compact_messages, estimate_message_tokens
from researchx.tools import create_research_tool_registry
from researchx.tools.base import ToolRegistry


@pytest.mark.parametrize(
    ("provider", "payload", "expected"),
    [
        ("openai", {"prompt_tokens": 100, "completion_tokens": 5}, (100, None, None, 0)),
        (
            "openai",
            {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}},
            (100, 0, None, 100),
        ),
        ("openai", {"prompt_tokens": 100, "prompt_cache_hit_tokens": 80}, (100, 80, None, 100)),
        (
            "openai",
            {
                "prompt_tokens": 100,
                "prompt_cache_hit_tokens": 80,
                "prompt_tokens_details": {"cached_tokens": 75},
            },
            (100, 75, None, 100),
        ),
        (
            "responses",
            {
                "input_tokens": 100,
                "input_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 30},
            },
            (100, 60, 30, 100),
        ),
        (
            "anthropic",
            {"input_tokens": 20, "cache_read_input_tokens": 70, "cache_creation_input_tokens": 10},
            (100, 70, 10, 100),
        ),
        ("anthropic", {"input_tokens": 20}, (20, None, None, 0)),
    ],
)
def test_provider_accounting(provider, payload, expected):
    # SDK objects and raw JSON have the same semantics.
    for raw in (payload, SimpleNamespace(**payload)):
        usage = usage_from_provider(raw, provider)
        assert (
            usage.input_tokens,
            usage.cache_read_input_tokens,
            usage.cache_creation_input_tokens,
            usage.cache_observed_input_tokens,
        ) == expected


def test_partial_legacy_usage_restore_and_zero():
    tracker = CostTracker(UsageSnapshot.model_validate({"input_tokens": 100, "output_tokens": 10}))
    tracker.add(usage_from_provider({"prompt_tokens": 100, "prompt_cache_hit_tokens": 0}, "openai"))
    assert tracker.total.cache_hit_rate == 0
    tracker.add(
        usage_from_provider({"prompt_tokens": 100, "prompt_cache_hit_tokens": 80}, "openai")
    )
    assert tracker.total.input_tokens == 300
    assert tracker.total.cache_hit_rate == 0.4
    assert tracker.total.cache_creation_input_tokens is None
    assert "partial" in tracker.total.cache_summary()
    assert (
        CostTracker(UsageSnapshot.model_validate(tracker.total.model_dump())).total == tracker.total
    )


@pytest.mark.parametrize("usage_only", [False, True])
async def test_stream_usage_final_or_repeated_counts_once(usage_only):
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 5,
        "total_tokens": 105,
        "prompt_cache_hit_tokens": 80,
        "prompt_tokens_details": {"cached_tokens": 80},
    }
    chunks = [
        {
            "id": "x",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "deepseek-flash",
            "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": usage,
        }
    ]
    if usage_only:
        chunks.append({**chunks[0], "choices": []})
    body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    client = OpenAICompatibleClient(api_key="test-key")
    await client._client._client.aclose()
    client._client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        events = [
            event
            async for event in client.stream_message(
                ApiMessageRequest(
                    model="deepseek-flash",
                    context_window_tokens=128_000,
                    messages=[ConversationMessage.from_user_text("hi")],
                    tools=[
                        {"name": "read", "description": "read", "input_schema": {"type": "object"}}
                    ],
                )
            )
        ]
        completed = [event for event in events if isinstance(event, ApiMessageCompleteEvent)]
        assert len(completed) == 1
        assert completed[0].usage.input_tokens == 100
        assert completed[0].usage.cache_read_input_tokens == 80
        assert "stream_options" not in seen[0]  # preserve compatibility behavior with tools
    finally:
        await client.close()


def test_split_prompt_stability(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCHX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("RESEARCHX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("CLAUDE_CODE_COORDINATOR_MODE", raising=False)
    env = get_environment_info(str(tmp_path))
    monkeypatch.setattr("researchx.prompts.context.get_environment_info", lambda **_: env)
    settings = Settings()
    first = build_runtime_prompt(settings, cwd=tmp_path)
    env = replace(env, date="2099-01-01")
    second = build_runtime_prompt(settings, cwd=tmp_path)
    assert first.system_prompt == second.system_prompt
    assert first.runtime_context != second.runtime_context
    assert "2099-01-01" not in second.system_prompt
    assert "2099-01-01" in second.runtime_context
    assert "Git:" not in second.runtime_context


class RecordingClient:
    def __init__(self, responses=None):
        self.requests = []
        self.responses = iter(responses or [])

    async def stream_message(self, request):
        self.requests.append(deepcopy(request))
        message = next(
            self.responses, ConversationMessage(role="assistant", content=[TextBlock(text="ok")])
        )
        yield ApiMessageCompleteEvent(message=message, usage=UsageSnapshot(input_tokens=10))


def make_engine(tmp_path, client, prompt=None):
    return QueryEngine(
        api_client=client,
        cwd=tmp_path,
        model="claude-sonnet-4-6",
        system_prompt=prompt or RuntimePrompt("Stable instructions", "State A"),
        settings=Settings(memory={"enabled": False}),
        tool_registry=create_research_tool_registry(),
        permission_checker=PermissionChecker(PermissionSettings()),
    )


@pytest.fixture(autouse=True)
def ordinary_session_mode(monkeypatch):
    """Cache tests exercise ordinary sessions, independent of prior coordinator tests."""
    monkeypatch.delenv("CLAUDE_CODE_COORDINATOR_MODE", raising=False)


def wire(messages):
    return (
        [m.to_api_param() for m in messages],
        _convert_messages_to_openai(messages, "stable"),
        _convert_messages_to_codex(messages),
    )


async def test_history_snapshot_stable_and_restore(tmp_path):
    client = RecordingClient()
    engine = make_engine(tmp_path, client)
    _ = [event async for event in engine.submit_message("first")]
    original = wire(engine.messages)
    engine.set_system_prompt(RuntimePrompt("Stable instructions", "State B"))
    _ = [event async for event in engine.submit_message("second")]
    assert client.requests[0].system_prompt == client.requests[1].system_prompt
    assert client.requests[0].tools == client.requests[1].tools
    assert wire(client.requests[1].messages[:2]) == original
    last_user = client.requests[1].messages[-1]
    assert last_user.text == "second"
    assert last_user.api_content()[-1].text.endswith("State B\n</runtime_context>")
    assert estimate_message_tokens([last_user]) > estimate_message_tokens(
        [ConversationMessage.from_user_text("second")]
    )
    restored = make_engine(tmp_path, RecordingClient())
    await restored.load_messages(
        [ConversationMessage.model_validate(m.model_dump()) for m in engine.messages]
    )
    restored.restore_usage(engine.total_usage.model_dump())
    assert wire(restored.messages) == wire(engine.messages)
    assert restored.total_usage.input_tokens == 20


async def test_tool_pair_then_mode_update_and_compaction(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCHX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("RESEARCHX_DATA_DIR", str(tmp_path / "data"))
    settings = Settings(memory={"enabled": False})
    client = RecordingClient(
        [
            ConversationMessage(
                role="assistant",
                content=[ToolUseBlock(id="read", name="glob", input={"pattern": "*"})],
            )
        ]
    )
    engine = make_engine(tmp_path, client, build_runtime_prompt(settings, cwd=tmp_path))
    engine.tool_metadata["permission_mode"] = "plan"
    _ = [event async for event in engine.submit_message("plan")]
    second = client.requests[1].messages
    use_index = next(i for i, m in enumerate(second) if m.tool_uses)
    assert isinstance(second[use_index + 1].content[0], ToolResultBlock)
    assert second[0].runtime_context and "当前已启用计划模式" in second[0].runtime_context
    assert second[-1].text == ""
    assert client.requests[0].system_prompt == client.requests[1].system_prompt
    latest = second[0].runtime_context
    (await engine.load_messages(
        compact_messages(engine.messages, preserve_recent=1), preserve_runtime_context=True
    ))
    assert any(m.runtime_context == latest for m in engine.messages)
    _ = [event async for event in engine.continue_pending()]
    assert any(m.runtime_context == latest for m in client.requests[-1].messages)


async def test_cancel_during_tool_loop_retains_results(tmp_path):
    client = RecordingClient(
        [
            ConversationMessage(
                role="assistant",
                content=[ToolUseBlock(id="read", name="glob", input={"pattern": "*"})],
            )
        ]
    )
    engine = make_engine(tmp_path, client)
    from researchx.engine.stream_events import AssistantTurnComplete

    stream = engine.submit_message("list")
    async for event in stream:
        # Interrupt before execution.
        if isinstance(event, AssistantTurnComplete):
            break
    await stream.aclose()
    before = engine.messages[0].runtime_context
    _ = [event async for event in engine.continue_pending()]
    assert engine.messages[0].runtime_context == before
    messages = client.requests[-1].messages
    use_index = next(i for i, message in enumerate(messages) if message.tool_uses)
    result = messages[use_index + 1].content[0]
    assert isinstance(result, ToolResultBlock) and result.tool_use_id == "read"
    assert result.is_error and "No durable receipt exists" in result.content


def test_tools_deterministic_order():
    tools = create_research_tool_registry().list_tools()
    a, b = ToolRegistry(), ToolRegistry()
    for tool in tools:
        a.register(tool)
    for tool in reversed(tools):
        b.register(tool)
    assert json.dumps(a.to_api_schema()) == json.dumps(b.to_api_schema())


def test_reasoning_replay_survives_session_serialization():
    message = ConversationMessage(
        role="assistant",
        content=[TextBlock(text="answer")],
        reasoning_content="provider replay",
    )
    restored = ConversationMessage.model_validate(message.model_dump(mode="json"))
    assert wire([restored]) == wire([message])
    assert restored.text == "answer"


def test_mcp_schema_property_order_does_not_change_wire():
    from researchx.mcp.types import McpToolInfo
    from researchx.tools.mcp_tool import McpToolAdapter

    registries = []
    for properties in (
        {"start": {"type": "integer"}, "end": {"type": "integer"}},
        {"end": {"type": "integer"}, "start": {"type": "integer"}},
    ):
        registry = ToolRegistry()
        info = McpToolInfo(
            server_name="example",
            name="history",
            description="history",
            input_schema={"type": "object", "properties": properties, "required": list(properties)},
        )
        registry.register(McpToolAdapter(None, info))
        registries.append(registry)
    assert json.dumps(registries[0].to_api_schema()) == json.dumps(registries[1].to_api_schema())


async def test_pending_results_remain_resumable_after_hidden_state_update(tmp_path):
    engine = make_engine(tmp_path, RecordingClient())
    (await engine.load_messages(
        [
            ConversationMessage(
                role="assistant", content=[ToolUseBlock(id="read", name="glob", input={})]
            ),
            ConversationMessage(
                role="user", content=[ToolResultBlock(tool_use_id="read", content="done")]
            ),
            ConversationMessage(role="user", runtime_context="new state"),
        ]
    ))
    assert engine.has_pending_continuation()


async def test_persisted_context_not_in_transcript_or_title(tmp_path, monkeypatch):
    from researchx.services.sessions.storage import (
        save_session_snapshot,
        load_session_snapshot,
        export_session_markdown,
    )

    monkeypatch.setenv("RESEARCHX_DATA_DIR", str(tmp_path / "data"))
    message = ConversationMessage(
        role="user", content=[TextBlock(text="Research")], runtime_context="PRIVATE_REFERENCE"
    )
    (await save_session_snapshot(
        cwd=tmp_path,
        model="claude-sonnet-4-6",
        system_prompt="stable",
        messages=[message],
        usage=UsageSnapshot(input_tokens=10),
    ))
    saved = (await load_session_snapshot(tmp_path))
    assert saved["summary"] == "Research"
    assert saved["messages"][0]["runtime_context"] == "PRIVATE_REFERENCE"
    transcript = export_session_markdown(cwd=tmp_path, messages=[message]).read_text()
    assert "Research" in transcript
    assert "PRIVATE_REFERENCE" not in transcript
