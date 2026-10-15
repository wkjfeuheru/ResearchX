"""Business delivery gates: source rights, coverage and independent human decisions."""

from datetime import datetime, timedelta, timezone
from datetime import date
import hashlib

import pytest

from researchx.state.business_coverage import classify_request, evaluate_coverage
from researchx.state.deliveries import DeliveryService, TrustedReviewer
from researchx.state.errors import ResearchError
from researchx.state.models import Evidence, ResearchMemory, SourceRecord, SourceUsage
from researchx.state.source_policy import collected_usage, may_enter_external_model, history_use_gap
from researchx.state.store import ResearchStore
from researchx.contracts.models import FactBasis
from tests.test_research.test_store import apply, reasoning


def _source(identity: str, locator: str, *, original: str | None = None, fragment: bool = False) -> SourceRecord:
    published = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    return SourceRecord(
        id=identity, kind="web", title="公告或新闻", locator=locator,
        origin_id=identity, collected_at=datetime.now(timezone.utc).isoformat(),
        published_at=published, content_hash=hashlib.sha256(identity.encode()).hexdigest(), snapshot="sha256:" + "a" * 64,
        fragment=fragment, original_locator=original,
        usage=SourceUsage(category="public", licence_status="pending",
                          model_use_allowed=True, internal_export_allowed=True,
                          completeness="fragment" if fragment else "full"),
    )


def test_business_profile_and_source_use_are_host_owned():
    assert classify_request("梳理最近新闻事件并点评") == "event_commentary"
    assert classify_request("分析最近财报新闻") == "ambiguous"
    assert not may_enter_external_model(SourceUsage())
    assert may_enter_external_model(collected_usage("web_search", {"kind": "search"}))
    assert may_enter_external_model(collected_usage(
        "web_fetch", {"kind": "web", "locator": "https://www.sse.com.cn/announcement"}
    ))
    assert not may_enter_external_model(collected_usage(
        "web_fetch", {"kind": "web", "locator": "https://www.caixin.com/article"}
    ))
    assert not may_enter_external_model(collected_usage("mcp", {"kind": "web", "usage": {
        "category": "public", "model_use_allowed": True,
    }}))
    assert may_enter_external_model(SourceUsage(category="licensed", licence_status="confirmed",
                                               model_use_allowed=True))
    assert not may_enter_external_model(SourceUsage(category="public", licence_status="restricted",
                                                   model_use_allowed=True))


def test_historical_tool_result_cannot_bypass_revoked_source_use():
    from researchx.engine.messages import ConversationMessage, ToolResultBlock

    source = _source("src_history", "https://www.sse.com.cn/announcement")
    memory = ResearchMemory(session_id="history", sources={source.id: source})
    messages = [ConversationMessage(role="user", content=[ToolResultBlock(
        tool_use_id="old-call", content="原始资料", result_metadata={"research_sources": [source.id]},
    )])]
    assert history_use_gap(memory, messages) is None
    source.usage = SourceUsage()
    assert history_use_gap(memory, messages)


def test_coverage_requires_checked_full_independent_origins():
    memory = ResearchMemory(session_id="coverage")
    official = _source("src_official", "https://www.sse.com.cn/announcement")
    media = _source("src_media", "https://www.stcn.com/article")
    duplicate = _source("src_copy", "https://www.yicai.com/repost",
                        original="https://www.stcn.com/article")
    for source in (official, media, duplicate):
        memory.sources[source.id] = source
        memory.evidence_pool[source.id] = Evidence(
            id=source.id, statement=source.title, source_id=source.id,
            collected_at=source.collected_at, status="source_checked",
        )
    coverage = evaluate_coverage(memory, "event_commentary", "最近新闻")
    assert coverage.status == "complete"
    assert next(item for item in coverage.channels if item.name == "independent_news").source_ids == ["src_media"]
    failed = evaluate_coverage(memory, "event_commentary", "最近新闻", attempts=[
        {"channel": "disclosure", "outcome": "error", "errors": ["timeout"], "created_at": 1},
    ])
    assert failed.status == "partial" and any("渠道查询失败" in gap for gap in failed.gaps)
    memory.sources[official.id].fragment = True
    coverage = evaluate_coverage(memory, "event_commentary", "最近新闻")
    assert coverage.status == "partial"
    assert "official_disclosure" in coverage.gaps[0]


def test_financial_coverage_requires_comparable_fact_basis():
    memory = ResearchMemory(session_id="financial")
    cutoff = datetime.now(timezone.utc)
    for year in (2024, 2025):
        source = _source(f"src_{year}", f"https://www.sse.com.cn/report/{year}")
        source.title = f"{year}年度报告"
        memory.sources[source.id] = source
        basis = FactBasis(
            security_code="600000", market="SSE", period_start=date(year, 1, 1),
            period_end=date(year, 12, 31), period_type="annual", scope="consolidated",
            currency="CNY", unit="亿元", adjustment="as_reported", value_kind="actual",
            as_of=cutoff, published_at=cutoff - timedelta(days=1),
        )
        memory.evidence_pool[source.id] = Evidence(
            id=source.id, statement="营业收入", source_id=source.id,
            collected_at=source.collected_at, status="source_checked", fact_basis=basis,
        )
    assert evaluate_coverage(memory, "financial_commentary", "财报点评").status == "complete"
    memory.evidence_pool["src_2024"].fact_basis.unit = "万元"
    coverage = evaluate_coverage(memory, "financial_commentary", "财报点评")
    assert coverage.status == "partial"
    assert "comparable_period_report" in coverage.gaps[0]


async def _checked_source(store: ResearchStore, origin: str, locator: str) -> str:
    published = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    source = await store.capture(
        origin_id=origin, kind="web", content=f"{origin}：完整可核对的原始公告或独立新闻。",
        title="公告或新闻", locator=locator, published_at=published,
        usage=SourceUsage(category="public", licence_status="pending",
                          model_use_allowed=True, internal_export_allowed=True,
                          completeness="full"),
    )
    evidence = (await apply(store, "add_evidence", source_id=source.id,
                            statement="已取得原文"))["evidence_id"]
    step = await reasoning(store, [evidence], verification=True)
    return (await apply(store, "verify_evidence", evidence_id=evidence,
                        level="source_checked", method="source",
                        verification_step_id=step,
                        verification_note="已逐项核对完整原文"))["evidence_id"]


async def test_review_is_version_bound_and_source_changes_invalidate(tmp_path):
    store = ResearchStore(tmp_path, "a" * 12, root=tmp_path / "memory")
    official = await _checked_source(store, "official", "https://www.sse.com.cn/announcement")
    media = await _checked_source(store, "media", "https://www.stcn.com/article")
    answer_id = "answer_review"
    await store.render_answer(f"事件点评 [E:{official}] [E:{media}]", answer_id)
    delivery = await DeliveryService(store).freeze_answer(answer_id, "梳理最近新闻事件并点评")
    assert delivery is not None and delivery["status"] == "awaiting_review"
    assert await DeliveryService(store).freeze_answer(answer_id, "梳理最近新闻事件并点评") == delivery
    with pytest.raises(ResearchError, match="可信审核人"):
        await DeliveryService(store).decide(
            str(delivery["delivery_id"]), "approve", "已核对", principal=None,
            expected_manifest_hash=str(delivery["manifest_hash"]),
        )
    reviewer = TrustedReviewer("researcher-1", "researcher", "trusted_identity_provider")
    approved = await DeliveryService(store).decide(
        str(delivery["delivery_id"]), "approve", "已核对原文及缺口", principal=reviewer,
        expected_manifest_hash=str(delivery["manifest_hash"]),
    )
    assert approved["status"] == "approved"
    source_id = (await store.load()).evidence_pool[official].source_id
    await DeliveryService(store).classify_source(
        source_id,
        SourceUsage(category="internal", provider="restricted", licence_status="restricted"),
        principal=TrustedReviewer("compliance-1", "compliance", "trusted_identity_provider"),
    )
    reopened = ResearchStore(tmp_path, "a" * 12, root=tmp_path / "memory")
    items = await DeliveryService(reopened).list()
    assert items[0]["status"] == "invalidated"
    with pytest.raises(ResearchError, match="Source licence"):
        await DeliveryService(reopened).export_text(str(delivery["delivery_id"]))


async def test_legacy_source_freezes_only_as_blocked_draft(tmp_path):
    store = ResearchStore(tmp_path, "b" * 12, root=tmp_path / "memory")
    source = await store.capture(origin_id="legacy", kind="file", content="旧资料", locator="file:old")
    with pytest.raises(ResearchError, match="来源授权"):
        await store.apply({"action": "read", "ids": [source.id], "include_content": True}, model_access=True)
    evidence = (await apply(store, "add_evidence", source_id=source.id,
                            statement="旧资料"))["evidence_id"]
    await store.render_answer(f"旧资料点评 [E:{evidence}]", "legacy_answer")
    delivery = await DeliveryService(store).freeze_answer("legacy_answer", "最近新闻点评")
    assert delivery is not None
    assert delivery["status"] == "blocked"
    assert delivery["quality"]["completeness"] == "blocked"
    assert delivery["quality"]["execution_state"] == "succeeded"
    assert any("重新分类" in gap for gap in delivery["coverage"]["gaps"])


async def test_unreferenced_old_evidence_cannot_fill_current_delivery_gaps(tmp_path):
    store = ResearchStore(tmp_path, "e" * 12, root=tmp_path / "memory")
    await _checked_source(store, "old_official", "https://www.sse.com.cn/announcement")
    news = await _checked_source(store, "new_media", "https://www.stcn.com/article")
    await store.render_answer(f"当前新闻 [E:{news}]", "new_answer")
    delivery = await DeliveryService(store).freeze_answer("new_answer", "最近新闻点评")
    assert delivery["coverage"]["status"] == "partial"
    assert delivery["quality"]["completeness"] == "partial"
    assert delivery["quality"]["execution_state"] == "succeeded"
    assert any("official_disclosure" in gap for gap in delivery["coverage"]["gaps"])


async def test_untrusted_tool_cannot_send_source_to_next_model_attempt(tmp_path):
    from tests.test_research.test_engine import LargeSourceTool, OneToolModel, engine
    from researchx.engine.messages import ToolResultBlock

    class UntrustedSource(LargeSourceTool):
        name = "untrusted_source"

    store = ResearchStore(tmp_path, "d" * 12)
    model = OneToolModel("untrusted_source")
    agent = engine(tmp_path, store, UntrustedSource(), model)
    _ = [event async for event in agent.submit_message("读取资料")]
    assert len(model.requests) == 2
    results = [block for message in model.requests[-1].messages for block in message.content
               if isinstance(block, ToolResultBlock)]
    assert results and results[0].is_error
    assert all("original financial report" not in block.content for block in results)


async def test_binary_skill_file_and_answer_share_delivery_versions(tmp_path):
    store = ResearchStore(tmp_path, "c" * 12, root=tmp_path / "memory")
    source = await store.capture(
        origin_id="public", kind="web", content="公开资料", locator="https://www.sse.com.cn/test",
        usage=SourceUsage(category="public", licence_status="pending", model_use_allowed=True,
                          internal_export_allowed=True, completeness="full"),
    )
    service = DeliveryService(store)
    delivery = await service.freeze_file("test-docx", b"PK\x00binary-docx", evidence_ids=[], source_ids=[source.id])
    assert delivery["kind"] == "report_file" and delivery["status"] == "blocked"
    assert await service.export_text(str(delivery["delivery_id"])) == b"PK\x00binary-docx"
    assert len(await service.list()) == 1


@pytest.mark.parametrize("changed", ["body", "evidence"])
async def test_approved_version_invalidated_by_dependency_changes(tmp_path, changed):
    store = ResearchStore(tmp_path, "f" * 12, root=tmp_path / "memory")
    official = await _checked_source(store, "official", "https://www.sse.com.cn/announcement")
    news = await _checked_source(store, "news", "https://www.stcn.com/article")
    await store.render_answer(f"点评 [E:{official}] [E:{news}]", "versioned_answer")
    service = DeliveryService(store)
    delivery = await service.freeze_answer("versioned_answer", "最近新闻点评")
    await service.decide(delivery["delivery_id"], "approve", "已核对",
        principal=TrustedReviewer("reviewer", "researcher", "trusted_identity_provider"),
        expected_manifest_hash=delivery["manifest_hash"])
    async with store.transaction():
        memory = await store._load()
        if changed == "body":
            memory.answers["versioned_answer"]["rendered"] = "正文已变更"
        else:
            memory.evidence_pool[official].statement = "更正后的事实"
        await store._save(memory, "trusted_correction", {})
    assert (await service.list())[0]["status"] == "invalidated"


async def test_research_grep_does_not_expose_unclassified_files(tmp_path):
    from researchx.tools.grep_tool import GrepTool, GrepToolInput
    from researchx.tools.base import ToolExecutionContext

    store = ResearchStore(tmp_path, "1" * 12, root=tmp_path / "memory")
    path = tmp_path / "private.txt"
    path.write_text("INTERNAL_SECRET", encoding="utf-8")
    context = ToolExecutionContext(cwd=tmp_path, metadata={"research_store": store})
    result = await GrepTool().execute(GrepToolInput(pattern="SECRET", root=str(path)), context)
    assert "INTERNAL_SECRET" not in result.output
    assert "未确认" in result.output
    await store.capture(origin_id="authorised", kind="file", locator=str(path), content=path.read_text(),
        usage=SourceUsage(category="internal", licence_status="confirmed", model_use_allowed=True))
    result = await GrepTool().execute(GrepToolInput(pattern="SECRET", root=str(path)), context)
    assert "INTERNAL_SECRET" in result.output
    assert result.metadata["research_source_specs"][0]["fragment"]


async def test_shell_stdout_is_not_a_source_authorisation(tmp_path):
    from tests.test_research.test_engine import LargeSourceTool, OneToolModel, engine
    from researchx.tools.base import ToolResult
    from researchx.engine.messages import ToolResultBlock

    class ShellOutput(LargeSourceTool):
        name = "bash"

        async def execute(self, arguments, context):
            return ToolResult(output="UNCLASSIFIED_SHELL_DATA", metadata={"returncode": 0})

    store = ResearchStore(tmp_path, "2" * 12)
    model = OneToolModel("bash")
    agent = engine(tmp_path, store, ShellOutput(), model)
    _ = [event async for event in agent.submit_message("执行计算")]
    results = [block for message in model.requests[-1].messages for block in message.content
               if isinstance(block, ToolResultBlock)]
    assert results and not results[0].is_error
    assert "UNCLASSIFIED_SHELL_DATA" not in results[0].content
    assert "原始输出" in results[0].content


async def test_compaction_cannot_launder_restricted_source(tmp_path):
    from researchx.engine.messages import ConversationMessage, ToolResultBlock
    from researchx.services.compact import compact_conversation
    from researchx.services.context.budget import ContextBudgetError

    store = ResearchStore(tmp_path, "3" * 12)
    source = await store.capture(origin_id="private", kind="file", content="PRIVATE_CONTENT")
    original = [ConversationMessage(role="user", content=[ToolResultBlock(
        tool_use_id="read", content="PRIVATE_CONTENT", result_metadata={"research_sources": [source.id]},
    )])]
    with pytest.raises(ContextBudgetError, match="授权"):
        await compact_conversation(original, api_client=object(), model="fixture",
                                   carryover_metadata={"research_store": store})
    memory = await store.load()
    for provenance in ({"source_ids": [source.id]}, None):
        summary = ConversationMessage.from_user_text("PRIVATE_CONTENT", context_origin="compaction")
        summary.research_citations = provenance
        assert history_use_gap(memory, [summary])


async def test_legacy_opaque_tool_result_cannot_enter_model(tmp_path, monkeypatch):
    from tests.test_research.test_engine import LargeSourceTool, OneToolModel, engine
    from researchx.tools.base import ToolResult

    class OpaqueTool(LargeSourceTool):
        name = "opaque_connector"

        async def execute(self, arguments, context):
            return ToolResult(output="UNCLASSIFIED_CONNECTOR_DATA")

    store = ResearchStore(tmp_path, "4" * 12)
    model = OneToolModel("opaque_connector")
    agent = engine(tmp_path, store, OpaqueTool(), model)
    from researchx.hooks import HookExecutor, HookExecutionContext, HookEvent
    from researchx.hooks.loader import HookRegistry
    from researchx.hooks.types import AggregatedHookResult

    hook_inputs = []

    async def observe_hook(self, event, payload, **kwargs):
        if event == HookEvent.POST_TOOL_USE:
            hook_inputs.append(payload["tool_output"])
        return AggregatedHookResult()

    monkeypatch.setattr(HookExecutor, "execute", observe_hook)
    agent._hook_executor = HookExecutor(HookRegistry(), HookExecutionContext(tmp_path, model, "fixture"))
    events = [event async for event in agent.submit_message("读取资料")]
    assert len(model.requests) == 1
    assert any("授权" in getattr(event, "message", "") for event in events)
    assert hook_inputs and all("UNCLASSIFIED_CONNECTOR_DATA" not in item for item in hook_inputs)
