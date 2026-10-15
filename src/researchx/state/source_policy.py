"""Host-owned source-use decisions. Tool metadata cannot grant its own rights."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit

from researchx.config.sites import classify_source, get_research_sites
from researchx.state.models import SourceUsage

PUBLIC_NEWS_HOSTS = frozenset({"stcn.com", "cs.com.cn", "cnstock.com", "yicai.com"})


def collected_usage(tool_name: str, spec: Mapping[str, object]) -> SourceUsage:
    """Only built-in public collection may mark data usable by an external model."""
    if (
        spec.get("kind") == "tool"
        and tool_name
        in {
            "write_file",
            "edit_file",
            "glob",
            "list_mcp_resources",
            "image_to_text",
            "skill",
            "tool_search",
            "sleep",
        }
    ) or (spec.get("kind") == "calculation" and tool_name == "bash"):
        # These built-in paths return operation acknowledgements, resource
        # listings, or output already checked/withheld at their input boundary.
        return SourceUsage(
            category="internal",
            provider="builtin_operation_context",
            licence_status="confirmed",
            model_use_allowed=True,
            completeness="fragment",
        )
    if tool_name in {"dispatch_subagents", "read_file", "grep"} and isinstance(
        spec.get("__host_usage"), dict
    ):
        return SourceUsage.model_validate(spec["__host_usage"])
    if tool_name == "web_search" and spec.get("kind") == "search":
        return SourceUsage(
            category="public",
            provider="web_search",
            licence_status="pending",
            model_use_allowed=True,
            internal_export_allowed=True,
            completeness="summary",
        )
    if tool_name == "web_fetch" and spec.get("kind") == "web":
        content = str(spec.get("content", "")).casefold()
        if any(
            marker in content
            for marker in (
                "订阅后阅读全文",
                "登录后阅读全文",
                "付费阅读",
                "subscribe to read",
                "subscription required",
            )
        ):
            return SourceUsage(provider="web_fetch", completeness="fragment")
        locator = str(spec.get("locator", ""))
        parsed = urlsplit(locator)
        source = classify_source(locator, get_research_sites())
        host = (parsed.hostname or "").lower()
        public_news = any(
            host == domain or host.endswith("." + domain) for domain in PUBLIC_NEWS_HOSTS
        )
        if (
            parsed.scheme == "https"
            and source["listed"]
            and (source["tier"] == "official" or public_news)
        ):
            return SourceUsage(
                category="public",
                provider=source["name"] or parsed.hostname or "",
                licence_status="pending",
                model_use_allowed=True,
                internal_export_allowed=True,
                completeness="fragment" if spec.get("fragment") else "full",
            )
    return SourceUsage(
        provider=tool_name,
        completeness="fragment" if spec.get("fragment") else "unknown",
    )


def may_enter_external_model(usage: SourceUsage) -> bool:
    return usage.model_use_allowed and (
        (usage.category == "public" and usage.licence_status in {"pending", "confirmed"})
        or (usage.category in {"licensed", "internal"} and usage.licence_status == "confirmed")
    )


def may_export(usage: SourceUsage) -> bool:
    return (
        usage.category != "unclassified"
        and usage.internal_export_allowed
        and (
            usage.licence_status == "confirmed"
            or (usage.category == "public" and usage.licence_status == "pending")
        )
    )


def history_source_ids(memory: object, messages: Sequence[object]) -> set[str]:
    """Host provenance retained across ordinary messages and compaction."""
    from researchx.engine.messages import ConversationMessage, ToolResultBlock
    from researchx.state.models import ResearchMemory

    assert isinstance(memory, ResearchMemory)
    referenced: set[str] = set()
    for message in messages:
        assert isinstance(message, ConversationMessage)
        for block in message.content:
            if isinstance(block, ToolResultBlock):
                ids = block.result_metadata.get("research_sources", [])
                if isinstance(ids, list):
                    referenced.update(key for key in ids if isinstance(key, str))
        citations = (message.research_citations or {}).get("citations", {})
        referenced.update((message.research_citations or {}).get("source_ids", []))
        if isinstance(citations, (dict, list)):
            referenced.update(
                memory.evidence_pool[key].source_id
                for key in citations
                if key in memory.evidence_pool
            )
    return referenced


def history_use_gap(memory: object, messages: Sequence[object]) -> str | None:
    """A previously allowed tool response must not bypass a later rights revocation."""
    from researchx.state.models import ResearchMemory

    assert isinstance(memory, ResearchMemory)
    referenced = history_source_ids(memory, messages)
    from researchx.engine.messages import ConversationMessage, TextBlock

    if any(
        isinstance(message, ConversationMessage)
        and message.context_origin == "compaction"
        and any(
            isinstance(block, TextBlock) and block.context_component is None
            for block in message.content
        )
        and "source_ids" not in (message.research_citations or {})
        for message in messages
    ):
        # Older summaries did not retain a provenance map. They cannot be used
        # to launder a source whose usage has never been classified.
        referenced.update(memory.sources)
    if any(
        source.kind != "user" and not source.is_error and not may_enter_external_model(source.usage)
        for key in referenced
        if (source := memory.sources.get(key)) is not None
    ):
        return "历史消息包含未授权或授权已变更的资料；须重新完成来源分类后才能继续调用模型。"
    return None


def image_use_allowed(
    memory: object, digest: str, approved_artifact_hashes: set[str] | None = None
) -> bool:
    from researchx.state.models import ResearchMemory

    assert isinstance(memory, ResearchMemory)
    if any(
        item.content_hash == digest
        and (item.kind == "user" or may_enter_external_model(item.usage))
        for item in memory.sources.values()
    ):
        return True
    return digest in (approved_artifact_hashes or set())


async def request_policy_view(
    workspace_id: str,
    session_id: str,
    messages: Sequence[object],
    image_hashes: set[str],
) -> tuple[object, set[str]]:
    """Load only provenance needed to check the next provider request.

    Legacy compacted messages without a source map require all source
    classifications. Normal requests select cited IDs and image candidates.
    """
    from sqlalchemy import or_, select

    from researchx.engine.messages import ConversationMessage, TextBlock, ToolResultBlock
    from researchx.state.episodes import EpisodicRepository
    from researchx.state.errors import ResearchError
    from researchx.state.models import Evidence, ResearchArtifact, ResearchMemory, SourceRecord
    from researchx.storage import schema as s
    from researchx.storage.database import current_database

    source_ids: set[str] = set()
    evidence_ids: set[str] = set()
    legacy_compaction = False
    for message in messages:
        assert isinstance(message, ConversationMessage)
        for block in message.content:
            if isinstance(block, ToolResultBlock):
                ids = block.result_metadata.get("research_sources", [])
                if isinstance(ids, list):
                    source_ids.update(key for key in ids if isinstance(key, str))
        citations = message.research_citations or {}
        raw_sources = citations.get("source_ids", [])
        if isinstance(raw_sources, list):
            source_ids.update(key for key in raw_sources if isinstance(key, str))
        raw_citations = citations.get("citations", {})
        if isinstance(raw_citations, (dict, list)):
            evidence_ids.update(key for key in raw_citations if isinstance(key, str))
        legacy_compaction |= (
            message.context_origin == "compaction"
            and any(
                isinstance(block, TextBlock) and block.context_component is None
                for block in message.content
            )
            and "source_ids" not in citations
        )

    memory = ResearchMemory(session_id=session_id)
    approved_artifact_hashes: set[str] = set()
    async with current_database().transaction() as db:
        revision = await db.scalar(
            select(s.research_sessions.c.revision)
            .where(
                s.research_sessions.c.workspace_id == workspace_id,
                s.research_sessions.c.session_id == session_id,
            )
            .with_for_update(read=True)
        )
        if revision is None:
            from researchx.state.errors import ResearchError

            raise ResearchError("研究会话不存在")
        artifact_table = s.entities["artifacts"]
        if image_hashes:
            artifact_rows = (
                await db.scalars(
                    select(artifact_table.c.payload).where(
                        artifact_table.c.workspace_id == workspace_id,
                        artifact_table.c.session_id == session_id,
                        artifact_table.c.content_hash.in_(image_hashes),
                    )
                )
            ).all()
            memory.artifacts = {
                item.id: item
                for item in (ResearchArtifact.model_validate(row) for row in artifact_rows)
            }
            repository = EpisodicRepository(workspace_id, session_id)
            for artifact in memory.artifacts.values():
                if artifact.stale or not artifact.evidence_ids:
                    continue
                try:
                    _, records, source_refs = await repository.read_records(
                        [artifact.id], strict=True
                    )
                except ResearchError:
                    continue
                sources = source_refs[artifact.id]
                if sources and all(
                    source_id in records
                    and records[source_id][0] == "source"
                    and (
                        (source := SourceRecord.model_validate(records[source_id][1])).kind
                        == "user"
                        or may_enter_external_model(source.usage)
                    )
                    for source_id in sources
                ):
                    approved_artifact_hashes.add(artifact.content_hash)
        evidence_table = s.entities["evidence_pool"]
        if evidence_ids:
            evidence_rows = (
                await db.scalars(
                    select(evidence_table.c.payload).where(
                        evidence_table.c.workspace_id == workspace_id,
                        evidence_table.c.session_id == session_id,
                        evidence_table.c.record_id.in_(evidence_ids),
                    )
                )
            ).all()
            memory.evidence_pool = {
                item.id: item for item in (Evidence.model_validate(row) for row in evidence_rows)
            }
            source_ids.update(item.source_id for item in memory.evidence_pool.values())
        source_table = s.entities["sources"]
        if legacy_compaction or source_ids or image_hashes:
            criteria = [
                source_table.c.workspace_id == workspace_id,
                source_table.c.session_id == session_id,
            ]
            if not legacy_compaction:
                branches = []
                if source_ids:
                    branches.append(source_table.c.record_id.in_(source_ids))
                if image_hashes:
                    branches.append(source_table.c.content_hash.in_(image_hashes))
                criteria.append(or_(*branches))
            source_rows = (await db.scalars(select(source_table.c.payload).where(*criteria))).all()
            memory.sources = {
                item.id: item for item in (SourceRecord.model_validate(row) for row in source_rows)
            }
    return memory, approved_artifact_hashes
