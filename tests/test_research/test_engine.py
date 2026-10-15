"""Exercise provenance capture, cancellation, and compaction through the real engine."""

import asyncio

import httpx
import pytest
from pydantic import BaseModel

from researchx.api.client import ApiMessageCompleteEvent
from researchx.api.usage import UsageSnapshot
from researchx.config.settings import PermissionSettings
from researchx.engine.messages import (
    ConversationMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    sanitize_conversation_messages,
)
from researchx.engine.query_engine import QueryEngine
from researchx.permissions.checker import PermissionChecker
from researchx.state.store import ResearchStore
from researchx.services.compact import compact_conversation, microcompact_messages
from researchx.tools.base import BaseTool, ToolRegistry, ToolResult
from researchx.tools.web_fetch_tool import WebFetchTool, WebFetchToolInput
from researchx.tools.web_fetch_tool import _HTMLTextExtractor


class OneToolModel:
    def __init__(self, name="test_source", call_id="source-call"):
        self.name = name
        self.call_id = call_id
        self.calls = 0
        self.requests = []

    async def stream_message(self, request):
        self.requests.append(request)
        self.calls += 1
        if self.calls == 1:
            message = ConversationMessage(
                role="assistant", content=[ToolUseBlock(id=self.call_id, name=self.name, input={})]
            )
        else:
            message = ConversationMessage(role="assistant", content=[TextBlock(text="阶段性结果")])
        yield ApiMessageCompleteEvent(
            message=message, usage=UsageSnapshot(input_tokens=1, output_tokens=1)
        )


def test_publication_schema_keeps_date_precision_and_ignores_icon_titles():
    parser = _HTMLTextExtractor()
    parser.feed(
        '<head><title>正式财报</title><script type="application/ld+json">'
        '{"@graph":[{"@type":"Article","datePublished":"2024-10-31Z",'
        '"dateModified":"2026-08-19T18:19:18Z"}]}</script></head>'
        "<body><svg><title>Cookie icon</title></svg>正文</body>"
    )
    parser.close()
    assert " ".join(parser.title_parts) == "正式财报"
    assert parser.published_at == "2024-10-31"
    assert all("datePublished" not in part for part in parser.parts)
    modified_only = _HTMLTextExtractor()
    modified_only.feed(
        '<script type="application/ld+json">'
        '{"@type":"Article","dateModified":"2026-08-19T18:19:18Z"}</script>'
    )
    assert modified_only.published_at is None


class EmptyInput(BaseModel):
    pass


class LargeSourceTool(BaseTool):
    name = "web_fetch"
    description = "Test provenance"
    input_model = EmptyInput

    def is_read_only(self, arguments):
        return True

    async def execute(self, arguments, context):
        text = "original financial report\n" * 1000
        return ToolResult(
            output=text,
            metadata={
                "research_source_specs": [
                    {
                        "kind": "web",
                        "title": "原始报告",
                        "locator": "https://www.sse.com.cn/report",
                        "content": text,
                        "fragment": False,
                    }
                ]
            },
        )


def engine(tmp_path, store, tool, model=None):
    registry = ToolRegistry()
    registry.register(tool)
    return QueryEngine(
        api_client=model or OneToolModel(tool.name),
        tool_registry=registry,
        permission_checker=PermissionChecker(PermissionSettings()),
        cwd=tmp_path,
        model="test",
        context_window_tokens=200_000,
        system_prompt="Research instructions",
        max_turns=10,
        tool_metadata={"research_store": store},
    )


@pytest.mark.asyncio
async def test_capture_precedes_output_offload_and_microcompact(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCHX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESEARCHX_TOOL_OUTPUT_INLINE_CHARS", "256")
    monkeypatch.setenv("RESEARCHX_TOOL_OUTPUT_PREVIEW_CHARS", "128")
    store = ResearchStore(tmp_path, "a" * 12)
    agent = engine(tmp_path, store, LargeSourceTool())
    _ = [event async for event in agent.submit_message("读取资料")]
    source = next(source for source in (await store.load()).sources.values() if source.kind == "web")
    assert len((await store.read_source(source))) > 20000
    tool_result = next(
        block
        for message in agent.messages
        for block in message.content
        if isinstance(block, ToolResultBlock)
    )
    assert len(tool_result.content) < 2000
    assert "research_source_specs" not in tool_result.result_metadata
    assert source.id in tool_result.content
    (await microcompact_messages(agent.messages, keep_recent=0))
    assert len((await store.read_source(source))) > 20000


@pytest.mark.asyncio
@pytest.mark.parametrize("is_error", [False, True])
async def test_cancellation_keeps_captured_sources_and_completes_tool_pair(tmp_path, is_error):
    store = ResearchStore(tmp_path, "a" * 12, root=tmp_path / "memory")
    captured = asyncio.Event()

    class SlowTool(LargeSourceTool):
        async def execute(self, arguments, context):
            (await store.capture(
                origin_id="source-call", content="captured before cancellation", is_error=is_error
            ))
            captured.set()
            await asyncio.sleep(3600)

    agent = engine(tmp_path, store, SlowTool())

    async def consume():
        return [event async for event in agent.submit_message("研究")]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(captured.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    messages = sanitize_conversation_messages(agent.messages)
    assert any(message.tool_uses for message in messages)
    result = messages[-1].content[0]
    assert isinstance(result, ToolResultBlock) and result.tool_use_id == "source-call"
    assert "Retained research sources" in result.content
    assert (
        (await store.read_source((await store.load()).sources[result.result_metadata["research_sources"][0]]))
        == "captured before cancellation"
    )
    assert result.is_error is is_error


@pytest.mark.asyncio
async def test_planned_retrieval_requires_committed_active_task(tmp_path):
    store = ResearchStore(tmp_path, "a" * 12, root=tmp_path / "memory")
    user = (await store.capture(origin_id="user", content="研究行业", kind="user"))
    (await store.apply(
        {
            "action": "set_context",
            "operation_id": "context",
            "expected_revision": 1,
            "goal": "研究行业",
            "user_source_ids": [user.id],
        }
    ))
    plan = (await store.apply(
        {
            "action": "create_plan",
            "operation_id": "plan",
            "expected_revision": 2,
            "title": "研究",
            "tasks": ["采集资料"],
        }
    ))
    agent = engine(tmp_path, store, LargeSourceTool())
    _ = [event async for event in agent.submit_message("研究")]
    result = next(
        block
        for message in agent.messages
        for block in message.content
        if isinstance(block, ToolResultBlock)
    )
    assert result.is_error and "in_progress" in result.content
    assert not any(source.kind == "web" for source in (await store.load()).sources.values())
    (await store.apply(
        {
            "action": "update_task",
            "operation_id": "start",
            "expected_revision": (await store.load()).revision,
            "task_id": plan["tasks"][0]["id"],
            "status": "in_progress",
        }
    ))
    # A new logical request receives a fresh provider call ID, not the rejected operation ID.
    agent = engine(
        tmp_path, store, LargeSourceTool(), OneToolModel("web_fetch", call_id="retrieval-after-task-start")
    )
    _ = [event async for event in agent.submit_message("继续研究")]
    assert any(source.kind == "web" for source in (await store.load()).sources.values())


@pytest.mark.asyncio
async def test_full_compaction_uses_research_prompt_and_keeps_store(tmp_path):
    store = ResearchStore(tmp_path, "a" * 12, root=tmp_path / "memory")
    source = (await store.capture(origin_id="report", content="research source"))
    messages = [ConversationMessage.from_user_text(f"资料 {i}") for i in range(16)]
    messages[0].content = [TextBlock(text="历史资料 " * 2000)]
    requests = []

    class Summarizer:
        async def stream_message(self, request):
            requests.append(request)
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(
                    role="assistant", content=[TextBlock(text="<summary>研究仍待核验</summary>")]
                ),
                usage=UsageSnapshot(),
            )

    metadata = {
        "research_store": store,
        "recent_verified_work": ["must not become financial verification"],
    }
    result = await compact_conversation(
        messages,
        api_client=Summarizer(),
        model="test",
        context_window_tokens=200_000,
        carryover_metadata=metadata,
    )
    prompt = requests[0].messages[-1].text
    assert "用户目标" in prompt and "Files and Code Sections" not in prompt
    assert any(item.kind == "research_memory" for item in result.attachments)
    assert not any(item.kind == "recent_verified_work" for item in result.attachments)
    assert (await store.read_source((await store.load()).sources[source.id])) == "research source"


@pytest.mark.asyncio
async def test_web_metadata_uses_redirect_url_and_full_pretruncation_content(monkeypatch, tmp_path):
    body = (
        '<title>公司报告</title><meta property="article:published_time" content="2026-09-30T10:00:00Z">'
        + "<p>财务资料</p>" * 1000
    )

    async def fetch(*args, **kwargs):
        return httpx.Response(
            200,
            text=body,
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", "https://example.org/final"),
        )

    monkeypatch.setattr("researchx.tools.web_fetch_tool.fetch_public_http_response", fetch)
    from researchx.tools.base import ToolExecutionContext

    result = await WebFetchTool().execute(
        WebFetchToolInput(url="https://example.org/start", max_chars=500),
        ToolExecutionContext(cwd=tmp_path),
    )
    source = result.metadata["research_source_specs"][0]
    assert source["locator"] == "https://example.org/final"
    assert source["title"] == "公司报告"
    assert source["published_at"] == "2026-09-30T10:00:00Z"
    assert len(source["content"]) > 500 and "[truncated]" in result.output


@pytest.mark.asyncio
@pytest.mark.parametrize("repairs", [True, False])
async def test_unknown_citations_get_bounded_correction_without_publishing_draft(tmp_path, repairs):
    from tests.test_research.test_store import evidence
    from researchx.engine.stream_events import StatusEvent

    store = ResearchStore(tmp_path, "a" * 12, root=tmp_path / "memory")
    ev = (await evidence(store, "公开报告中的营收增长"))

    class CitationModel:
        def __init__(self):
            self.requests = []

        async def stream_message(self, request):
            self.requests.append(request)
            text = (
                f"营收增长 [E:{ev}]"
                if repairs and len(self.requests) > 1
                else "营收增长 [E:unknown]"
            )
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(role="assistant", content=[TextBlock(text=text)]),
                usage=UsageSnapshot(input_tokens=10, output_tokens=2),
            )

    model = CitationModel()
    agent = engine(tmp_path, store, LargeSourceTool(), model)
    events = [event async for event in agent.submit_message("研究营收")]
    assert len(model.requests) == (2 if repairs else 3)
    final = agent.messages[-1]
    assert "来源：" in final.text if repairs else "来源不可核验" in final.text
    assert len((await store.load()).answers) == 1
    assert agent.total_usage.input_tokens == 10 * len(model.requests)
    assert any(isinstance(event, StatusEvent) and event.discard_draft for event in events)
    assert not any(
        message.role == "assistant" and "[E:unknown]" in message.text
        for message in agent.messages[:-1]
    )
    assert any(
        "<research_answer_check>" in (message.runtime_context or "") for message in agent.messages
    )
    assert (
        not final.research_citations["invalid"]
        if repairs
        else final.research_citations["invalid"] == ["unknown"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("is_error", [False, True])
async def test_empty_search_specs_do_not_fall_back_to_generic_evidence(tmp_path, is_error):
    class EmptySearchTool(LargeSourceTool):
        async def execute(self, arguments, context):
            return ToolResult(
                output="No search results / channel unavailable",
                is_error=is_error,
                metadata={"research_source_specs": [], "outcome": "error" if is_error else "empty"},
            )

    store = ResearchStore(tmp_path, "a" * 12, root=tmp_path / "memory")
    agent = engine(tmp_path, store, EmptySearchTool())
    _ = [event async for event in agent.submit_message("搜索资料")]
    result = next(
        block
        for message in agent.messages
        for block in message.content
        if isinstance(block, ToolResultBlock)
    )
    assert result.result_metadata["research_sources"] == []
    assert not any(source.origin_id == "source-call" for source in (await store.load()).sources.values())


@pytest.mark.asyncio
async def test_final_verification_is_bounded_to_three_rounds(tmp_path):
    store = ResearchStore(tmp_path, "a" * 12, root=tmp_path / "memory")
    user = (await store.capture(origin_id="user", content="核验营收", kind="user"))
    (await store.apply(
        {
            "action": "set_context",
            "operation_id": "context",
            "expected_revision": 1,
            "goal": "核验营收",
            "user_source_ids": [user.id],
        }
    ))
    plan = (await store.apply(
        {
            "action": "create_plan",
            "operation_id": "plan",
            "expected_revision": 2,
            "title": "核验",
            "tasks": ["核对原文"],
        }
    ))
    task_id = plan["tasks"][0]["id"]
    (await store.apply(
        {
            "action": "update_task",
            "operation_id": "start",
            "expected_revision": 3,
            "task_id": task_id,
            "status": "in_progress",
        }
    ))
    source = (await store.capture(
        origin_id="full-report",
        content="完整年报披露营收增长",
        kind="web",
        locator="https://example.org/report",
        fragment=False,
    ))
    item = (await store.apply(
        {
            "action": "add_evidence",
            "operation_id": "evidence",
            "expected_revision": (await store.load()).revision,
            "source_id": source.id,
            "statement": "营收增长",
        }
    ))["evidence_id"]
    (await store.apply(
        {
            "action": "update_task",
            "operation_id": "complete",
            "expected_revision": (await store.load()).revision,
            "task_id": task_id,
            "status": "completed",
            "completion_note": "已登记完整原文，等待核验",
        }
    ))

    class UncooperativeModel:
        def __init__(self):
            self.requests = []

        async def stream_message(self, request):
            self.requests.append(request)
            text = f"营收增长 [E:{item}]"
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(role="assistant", content=[TextBlock(text=text)]),
                usage=UsageSnapshot(input_tokens=1, output_tokens=1),
            )

    model = UncooperativeModel()
    agent = engine(tmp_path, store, LargeSourceTool(), model)
    events = [event async for event in agent.submit_message("给出结论")]
    assert len(model.requests) == 4
    assert "待核验（尚未完成原文核对）" in agent.messages[-1].text
    from researchx.engine.stream_events import StatusEvent

    assert (
        sum(isinstance(event, StatusEvent) and "核对引用原文" in event.message for event in events)
        == 3
    )
