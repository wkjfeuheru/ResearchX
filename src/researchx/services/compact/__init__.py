"""Transactional context reduction: archive, build a candidate, then validate.

Production compaction never uses the legacy destructive truncation helpers.
"""

from __future__ import annotations
from researchx.config.context_components import ContextComponentsSettings
from researchx.api.client import SupportsStreamingMessages
from researchx.engine.metadata import ExecutionMetadata
from researchx.services.context.budget import RequestBudget
from researchx.services.context.sources import ContextSnapshot, refresh_runtime_messages

import asyncio
import inspect
import logging
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping, Any, Awaitable, Callable, Literal
from uuid import uuid4

from researchx.engine.messages import (
    ConversationMessage,
    ContentBlock,
    ImageBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    sanitize_conversation_messages,
)
from researchx.engine.stream_events import CompactProgressEvent
from researchx.hooks import HookEvent, HookExecutor
from researchx.state.prompt import RESEARCH_COMPACT_PROMPT as BASE_COMPACT_PROMPT
from researchx.services.execution.outputs import is_microcompactable_tool_result
from researchx.services.context.token_estimation import estimate_tokens
from researchx.services.context.budget import (
    ContextBudgetError,
    budget_limits,
    get_context_window as get_context_window,
    prepare_request,
    request_budget,
)
from researchx.services.context.snapshots import save_context_snapshot, save_tool_content

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (from Claude Code microCompact.ts / autoCompact.ts)
# ---------------------------------------------------------------------------

COMPACTABLE_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "bash",
        "grep",
        "glob",
        "web_search",
        "web_fetch",
        "edit_file",
        "write_file",
    }
)

TIME_BASED_MC_CLEARED_MESSAGE = "[Old tool result content cleared]"

# Auto-compact thresholds
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3
COMPACT_TIMEOUT_SECONDS = 25
MAX_COMPACT_STREAMING_RETRIES = 2
MAX_PTL_RETRIES = 3
CONTEXT_COLLAPSE_TEXT_CHAR_LIMIT = 2_400
CONTEXT_COLLAPSE_HEAD_CHARS = 900
CONTEXT_COLLAPSE_TAIL_CHARS = 500
MAX_COMPACT_ATTACHMENTS = 6
MAX_DISCOVERED_TOOLS = 12

# Microcompact defaults
DEFAULT_KEEP_RECENT = 5
DEFAULT_GAP_THRESHOLD_MINUTES = 60

# Token estimation padding (conservative)
TOKEN_ESTIMATION_PADDING = 4 / 3
_DEFAULT_VISION_IMAGE_TOKEN_ESTIMATE = 3_072

# Default context windows per model family
PTL_RETRY_MARKER = "[earlier conversation truncated for compaction retry]"
ERROR_MESSAGE_INCOMPLETE_RESPONSE = "Compaction interrupted before a complete summary was returned."

CompactTrigger = Literal["auto", "manual", "reactive"]
CompactProgressCallback = Callable[[CompactProgressEvent], Awaitable[None]]
CompactionKind = Literal["full"]


@dataclass
class CompactAttachment:
    """Structured compact asset carried across a compaction boundary."""

    kind: str
    title: str
    body: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CompactionResult:
    """Structured compaction result, inspired by Claude Code's result shape."""

    trigger: CompactTrigger
    compact_kind: CompactionKind
    boundary_marker: ConversationMessage
    summary_messages: list[ConversationMessage]
    messages_to_keep: list[ConversationMessage]
    attachments: list[CompactAttachment]
    hook_results: list[CompactAttachment]
    compact_metadata: dict[str, Any] = field(default_factory=dict)
    status: Literal["success", "unchanged", "failed"] = "success"
    runtime_messages: list[ConversationMessage] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


def estimate_message_tokens(messages: list[ConversationMessage]) -> int:
    """Estimate total tokens for a conversation, including the 4/3 padding."""
    total = 0
    image_token_estimate = _vision_token_budget_per_image()
    for msg in messages:
        for block in msg.api_content():
            if isinstance(block, TextBlock):
                total += estimate_tokens(block.text)
            elif isinstance(block, ToolResultBlock):
                total += estimate_tokens(block.content)
            elif isinstance(block, ToolUseBlock):
                total += estimate_tokens(block.name)
                total += estimate_tokens(str(block.input))
            elif isinstance(block, ImageBlock):
                total += image_token_estimate
    return int(total * TOKEN_ESTIMATION_PADDING)


def estimate_conversation_tokens(messages: list[ConversationMessage]) -> int:
    """Alias kept for backward compatibility."""
    return estimate_message_tokens(messages)


def _vision_token_budget_per_image() -> int:
    raw = os.environ.get("RESEARCHX_IMAGE_TOKEN_ESTIMATE", "").strip()
    if raw:
        try:
            return max(64, int(raw))
        except ValueError:
            log.warning("Ignoring invalid RESEARCHX_IMAGE_TOKEN_ESTIMATE=%r", raw)
    return _DEFAULT_VISION_IMAGE_TOKEN_ESTIMATE


def _replace_images_with_compaction_placeholders(
    messages: list[ConversationMessage],
) -> list[ConversationMessage]:
    """Strip image payloads from summarizer-only compact requests."""
    replaced: list[ConversationMessage] = []
    for message in messages:
        next_content: list[ContentBlock] = []
        changed = False
        for block in message.content:
            if isinstance(block, ImageBlock):
                changed = True
                label = block.source_path.strip() or "inline"
                next_content.append(
                    TextBlock(
                        text=f"[Image omitted from compaction summarization; source: {label}.]\n"
                    )
                )
            else:
                next_content.append(block)
        if changed:
            replaced.append(message.model_copy(update={"content": next_content}))
        else:
            replaced.append(message)
    return replaced


def _sanitize_metadata(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _sanitize_metadata(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_metadata(item) for item in value]
    return str(value)


def _record_compact_checkpoint(
    carryover_metadata: ExecutionMetadata | None,
    *,
    checkpoint: str,
    trigger: CompactTrigger,
    message_count: int,
    token_count: int,
    attempt: int | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "checkpoint": checkpoint,
        "trigger": trigger,
        "message_count": message_count,
        "token_count": token_count,
    }
    if attempt is not None:
        payload["attempt"] = attempt
    if details:
        payload.update(_sanitize_metadata(details))
    if carryover_metadata is not None:
        checkpoints = carryover_metadata.setdefault("compact_checkpoints", [])
        if isinstance(checkpoints, list):
            checkpoints.append(payload)
        carryover_metadata["compact_last"] = payload
    return payload


async def _emit_progress(
    callback: CompactProgressCallback | None,
    *,
    phase: Literal[
        "hooks_start",
        "context_collapse_start",
        "context_collapse_end",
        "compact_start",
        "compact_retry",
        "compact_end",
        "compact_failed",
    ],
    trigger: CompactTrigger,
    message: str | None = None,
    attempt: int | None = None,
    checkpoint: str | None = None,
    metadata: Mapping[str, object] | None = None,
) -> None:
    if callback is None:
        return
    await callback(
        CompactProgressEvent(
            phase=phase,
            trigger=trigger,
            message=message,
            attempt=attempt,
            checkpoint=checkpoint,
            metadata=_sanitize_metadata(metadata) if metadata else None,
        )
    )


def _is_prompt_too_long_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        needle in text
        for needle in (
            "prompt too long",
            "context_length_exceeded",
            "context length",
            "maximum context",
            "context window",
            "input tokens exceed",
            "messages resulted in",
            "reduce the length of the messages",
            "configured limit",
            "too many tokens",
            "too large for the model",
            "maximum context length",
            "exceed_context",
            "exceeds the available context size",
            "available context size",
        )
    )


def _group_messages_by_prompt_round(
    messages: list[ConversationMessage],
) -> list[list[ConversationMessage]]:
    groups: list[list[ConversationMessage]] = []
    current: list[ConversationMessage] = []
    for message in messages:
        starts_new_round = (
            message.role == "user"
            and not any(isinstance(block, ToolResultBlock) for block in message.content)
            and bool(message.text.strip())
        )
        if starts_new_round and current:
            groups.append(current)
            current = []
        current.append(message)
    if current:
        groups.append(current)
    return groups


def _collapse_text(text: str) -> str:
    if len(text) <= CONTEXT_COLLAPSE_TEXT_CHAR_LIMIT:
        return text
    omitted = len(text) - CONTEXT_COLLAPSE_HEAD_CHARS - CONTEXT_COLLAPSE_TAIL_CHARS
    head = text[:CONTEXT_COLLAPSE_HEAD_CHARS].rstrip()
    tail = text[-CONTEXT_COLLAPSE_TAIL_CHARS:].lstrip()
    return f"{head}\n...[collapsed {omitted} chars]...\n{tail}"


def try_context_collapse(
    messages: list[ConversationMessage],
    *,
    preserve_recent: int,
) -> list[ConversationMessage] | None:
    """Deterministically shrink oversized text blocks before full compact."""
    if len(messages) <= preserve_recent + 2:
        return None

    older, newer = _split_preserving_tool_pairs(messages, preserve_recent=preserve_recent)
    changed = False
    collapsed_older: list[ConversationMessage] = []
    for message in older:
        new_blocks: list[ContentBlock] = []
        for block in message.content:
            if isinstance(block, TextBlock):
                collapsed = _collapse_text(block.text)
                if collapsed != block.text:
                    changed = True
                new_blocks.append(TextBlock(text=collapsed))
            elif isinstance(block, ToolResultBlock):
                collapsed = _collapse_text(block.content)
                if collapsed != block.content:
                    changed = True
                new_blocks.append(
                    ToolResultBlock(
                        tool_use_id=block.tool_use_id,
                        content=collapsed,
                        is_error=block.is_error,
                    )
                )
            else:
                new_blocks.append(block)
        collapsed_older.append(message.model_copy(update={"content": new_blocks}))

    if not changed:
        return None

    result = [*collapsed_older, *newer]
    if (estimate_message_tokens(result)) >= (estimate_message_tokens(messages)):
        return None
    return result


def truncate_head_for_ptl_retry(
    messages: list[ConversationMessage],
) -> list[ConversationMessage] | None:
    """Drop the oldest prompt rounds when the compact request itself is too large."""
    groups = _group_messages_by_prompt_round(messages)
    if len(groups) < 2:
        return None

    drop_count = max(1, len(groups) // 5)
    drop_count = min(drop_count, len(groups) - 1)
    retained = [message for group in groups[drop_count:] for message in group]
    if not retained:
        return None
    if retained[0].role == "assistant":
        return [
            ConversationMessage.from_user_text(PTL_RETRY_MARKER, context_origin="continuation"),
            *retained,
        ]
    return retained


def _extract_attachment_paths(messages: list[ConversationMessage]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    path_pattern = re.compile(r"path:\s*([^)\\n]+)")
    attachment_pattern = re.compile(r"\[attachment:\s*([^\]]+)\]")
    for message in messages:
        for block in message.content:
            if isinstance(block, ImageBlock) and block.source_path:
                path = str(Path(block.source_path).expanduser())
                if path not in seen:
                    seen.add(path)
                    found.append(path)
            elif isinstance(block, TextBlock):
                for match in path_pattern.findall(block.text):
                    path = match.strip()
                    if path and path not in seen:
                        seen.add(path)
                        found.append(path)
                for match in attachment_pattern.findall(block.text):
                    path = match.strip()
                    if path and "download failed" not in path and path not in seen:
                        seen.add(path)
                        found.append(path)
            if len(found) >= MAX_COMPACT_ATTACHMENTS:
                return found
    return found


def _extract_discovered_tools(messages: list[ConversationMessage]) -> list[str]:
    discovered: list[str] = []
    seen: set[str] = set()
    for message in messages:
        for tool_use in message.tool_uses:
            if tool_use.name and tool_use.name not in seen:
                seen.add(tool_use.name)
                discovered.append(tool_use.name)
            if len(discovered) >= MAX_DISCOVERED_TOOLS:
                return discovered
    return discovered


def _create_attachment(
    kind: str, title: str, lines: list[str], *, metadata: Mapping[str, object] | None = None
) -> CompactAttachment | None:
    filtered = [line.rstrip() for line in lines if line and line.strip()]
    if not filtered:
        return None
    return CompactAttachment(
        kind=kind,
        title=title,
        body="\n".join(filtered),
        metadata=_sanitize_metadata(metadata or {}),
    )


def render_compact_attachment(attachment: CompactAttachment) -> ConversationMessage:
    """Serialize a structured compact attachment into a conversation message."""
    header = f"[Compact attachment: {attachment.kind}] {attachment.title}".strip()
    text = f"{header}\n{attachment.body}".strip()
    return ConversationMessage.from_user_text(
        text, context_origin="compaction", context_component="dynamic_context"
    )


def create_compact_boundary_message(metadata: dict[str, Any]) -> ConversationMessage:
    """Create a boundary marker message for post-compact conversation rebuild."""
    lines = [
        "[Compact boundary marker]",
        "Earlier conversation was compacted. Use the summary and preserved assets below as the continuity boundary.",
    ]
    trigger = str(metadata.get("trigger") or "").strip()
    compact_kind = str(metadata.get("compact_kind") or "").strip()
    pre_messages = metadata.get("pre_compact_message_count")
    pre_tokens = metadata.get("pre_compact_token_count")
    post_messages = metadata.get("post_compact_message_count")
    post_tokens = metadata.get("post_compact_token_count")
    if trigger:
        lines.append(f"Trigger: {trigger}")
    if compact_kind:
        lines.append(f"Compaction kind: {compact_kind}")
    if pre_messages is not None or pre_tokens is not None:
        lines.append(
            "Pre-compact footprint: "
            f"messages={pre_messages if pre_messages is not None else 'unknown'}, "
            f"tokens={pre_tokens if pre_tokens is not None else 'unknown'}"
        )
    if post_messages is not None or post_tokens is not None:
        lines.append(
            "Post-compact footprint: "
            f"messages={post_messages if post_messages is not None else 'unknown'}, "
            f"tokens={post_tokens if post_tokens is not None else 'unknown'}"
        )
    if metadata.get("snapshot_path"):
        lines.append(f"Original conversation snapshot: {metadata['snapshot_path']}")
    anchor = str(metadata.get("preserved_segment_anchor") or "").strip()
    if anchor:
        lines.append(f"Preserved segment anchor: {anchor}")
    return ConversationMessage.from_user_text(
        "\n".join(lines), context_origin="compaction", context_component="dynamic_context"
    )


def build_post_compact_messages(result: CompactionResult) -> list[ConversationMessage]:
    """Rebuild the post-compact message list in Claude Code's ordering."""
    if result.status != "success":
        return list(result.messages_to_keep)
    attachment_messages = [
        render_compact_attachment(attachment) for attachment in result.attachments
    ]
    hook_messages = [render_compact_attachment(attachment) for attachment in result.hook_results]
    return [
        result.boundary_marker,
        *result.summary_messages,
        *result.messages_to_keep,
        *attachment_messages,
        *hook_messages,
        *result.runtime_messages,
    ]


def _boundary_crosses_tool_pair(
    previous: ConversationMessage, current: ConversationMessage
) -> bool:
    """Return True when a preserve boundary would split a tool_use/result pair."""

    if previous.role != "assistant" or current.role != "user":
        return False
    pending_tool_ids = {block.id for block in previous.content if isinstance(block, ToolUseBlock)}
    if not pending_tool_ids:
        return False
    result_ids = {
        block.tool_use_id for block in current.content if isinstance(block, ToolResultBlock)
    }
    return bool(pending_tool_ids & result_ids)


def _split_preserving_tool_pairs(
    messages: list[ConversationMessage],
    *,
    preserve_recent: int,
) -> tuple[list[ConversationMessage], list[ConversationMessage]]:
    """Split older/newer segments without cutting through a tool_use/result pair.

    The preserved segment is also sanitized so trailing orphan tool_use blocks
    never survive the compaction boundary.
    """

    if len(messages) <= preserve_recent:
        return [], sanitize_conversation_messages((list(messages)))

    split_index = max(0, len(messages) - preserve_recent)
    while split_index > 0 and _boundary_crosses_tool_pair(
        messages[split_index - 1], messages[split_index]
    ):
        split_index -= 1

    older = list(messages[:split_index])
    newer = sanitize_conversation_messages((list(messages[split_index:])))
    return older, newer


def _sanitize_compaction_segments(result: CompactionResult) -> None:
    """Normalize summary+preserved messages into a provider-safe sequence."""

    if not result.summary_messages and not result.messages_to_keep:
        return
    combined = [*result.summary_messages, *result.messages_to_keep]
    sanitized = sanitize_conversation_messages(combined)
    summary_count = len(result.summary_messages)
    result.summary_messages = sanitized[:summary_count]
    result.messages_to_keep = sanitized[summary_count:]


def _create_recent_attachments_attachment_if_needed(
    attachment_paths: list[str],
) -> CompactAttachment | None:
    if not attachment_paths:
        return None
    return _create_attachment(
        "recent_attachments",
        "最近的本地附件",
        ["请在工作记忆中保留以下本地附件路径："] + [f"- {path}" for path in attachment_paths],
        metadata={"paths": attachment_paths},
    )


def create_invoked_skills_attachment_if_needed(
    invoked_skills: Any,
) -> CompactAttachment | None:
    if not isinstance(invoked_skills, list) or not invoked_skills:
        return None
    normalized = [str(skill).strip() for skill in invoked_skills[-8:] if str(skill).strip()]
    if not normalized:
        return None
    return _create_attachment(
        "invoked_skills",
        "本会话此前使用的技能",
        [
            "以下技能已被调用，可能仍会影响下一步：",
            "- " + ", ".join(normalized),
        ],
        metadata={"skills": normalized},
    )


def _create_hook_attachments(hook_note: str | None) -> list[CompactAttachment]:
    if not hook_note or not hook_note.strip():
        return []
    attachment = _create_attachment(
        "hook_results",
        "Compact hook notes",
        [hook_note.strip()],
        metadata={"note": hook_note.strip()},
    )
    return [attachment] if attachment is not None else []


async def _build_compact_attachments(
    messages: list[ConversationMessage],
    *,
    metadata: ExecutionMetadata | None,
    model: str = "",
) -> list[CompactAttachment]:
    metadata = metadata or {}
    attachments = []
    store = metadata.get("research_store")
    if store is not None:
        attachments.append(
            CompactAttachment(
                kind="research_memory",
                title="当前研究检查点",
                body=(
                    await store.prompt(
                        int(metadata.get("research_injection_budget", 6000)), model=model
                    )
                ),
            )
        )
    skills = create_invoked_skills_attachment_if_needed(metadata.get("invoked_skills"))
    if skills is not None:
        attachments.append(skills)
    paths = _create_recent_attachments_attachment_if_needed(_extract_attachment_paths(messages))
    if paths is not None:
        attachments.append(paths)
    return attachments


def _finalize_compaction_result(result: CompactionResult) -> CompactionResult:
    _sanitize_compaction_segments(result)
    messages = build_post_compact_messages(result)
    result.compact_metadata.setdefault("post_compact_message_count", len(messages))
    result.compact_metadata.setdefault(
        "post_compact_token_count", (estimate_message_tokens(messages))
    )
    result.boundary_marker = create_compact_boundary_message(result.compact_metadata)
    return result


def _metadata_has_checkpoint(metadata: ExecutionMetadata | None, checkpoint: str) -> bool:
    if metadata is None:
        return False
    checkpoints = metadata.get("compact_checkpoints")
    if not isinstance(checkpoints, list):
        return False
    return any(
        isinstance(entry, dict) and entry.get("checkpoint") == checkpoint for entry in checkpoints
    )


def _build_passthrough_compaction_result(
    messages: list[ConversationMessage],
    *,
    trigger: CompactTrigger,
    compact_kind: CompactionKind,
    metadata: Mapping[str, object] | None = None,
) -> CompactionResult:
    compact_metadata = {
        "trigger": trigger,
        "compact_kind": compact_kind,
        "pre_compact_message_count": len(messages),
        "pre_compact_token_count": (estimate_message_tokens(messages)),
        **_sanitize_metadata(metadata or {}),
    }
    result = CompactionResult(
        trigger=trigger,
        compact_kind=compact_kind,
        boundary_marker=create_compact_boundary_message(compact_metadata),
        summary_messages=[],
        messages_to_keep=(list(messages)),
        attachments=[],
        hook_results=[],
        compact_metadata=compact_metadata,
    )
    result.status = compact_metadata.get("status", "unchanged")
    return result


# ---------------------------------------------------------------------------
# Microcompact — clear old tool results to reduce tokens cheaply
# ---------------------------------------------------------------------------


def _collect_compactable_tool_ids(messages: list[ConversationMessage]) -> list[str]:
    """Walk messages and collect tool_use IDs whose results are compactable."""
    ordered_ids: list[str] = []
    tool_names: dict[str, str] = {}
    result_content: dict[str, str] = {}
    for msg in messages:
        for block in msg.content:
            if isinstance(block, ToolUseBlock):
                ordered_ids.append(block.id)
                tool_names[block.id] = block.name
            elif isinstance(block, ToolResultBlock):
                result_content[block.tool_use_id] = block.content
    return [
        tool_id
        for tool_id in ordered_ids
        if tool_names.get(tool_id, "") in COMPACTABLE_TOOLS
        or is_microcompactable_tool_result(
            tool_names.get(tool_id, ""),
            result_content.get(tool_id, ""),
        )
    ]


async def microcompact_messages(
    messages: list[ConversationMessage],
    *,
    keep_recent: int = DEFAULT_KEEP_RECENT,
    snapshot_path: str | None = None,
) -> tuple[list[ConversationMessage], int]:
    """Replace archived results on a copy; always retain recoverable references."""
    result = [m.model_copy(deep=True) for m in messages]
    ids = _collect_compactable_tool_ids(result)
    clear = set(ids[: -max(1, keep_recent)])
    protected = _latest_user_index(result)
    eligible = [
        (i, j, b)
        for i, m in enumerate(result)
        if i < protected and m.role == "user"
        for j, b in enumerate(m.content)
        if isinstance(b, ToolResultBlock)
        and b.tool_use_id in clear
        and not b.result_metadata.get("context_artifact")
    ]
    if not eligible:
        return result, 0
    snapshot_path = snapshot_path or str((await save_context_snapshot(messages)))
    for i, j, block in eligible:
        artifact = await save_tool_content(block.content)
        sources = block.result_metadata.get("research_sources", [])
        reference = (
            f"[Archived tool result] tool_use_id={block.tool_use_id}; is_error={block.is_error}\n"
            f"Full output: {artifact}\nresearch_sources: {sources}\nSnapshot: {snapshot_path}"
        )
        if block.result_metadata.get("tool_output_artifact"):
            reference += f"\nOriginal tool output: {block.result_metadata['tool_output_artifact']}"
        if estimate_tokens(reference) >= estimate_tokens(block.content):
            continue
        result[i].content[j] = block.model_copy(
            update={
                "content": reference,
                "result_metadata": {
                    **block.result_metadata,
                    "context_artifact": str(artifact),
                    "context_snapshot": snapshot_path,
                },
            }
        )
    return result, max(0, (estimate_message_tokens(messages)) - (estimate_message_tokens(result)))


# ---------------------------------------------------------------------------
# Full compact — LLM-based summarization
# ---------------------------------------------------------------------------

NO_TOOLS_PREAMBLE = """\
重要：只能返回纯文本，不要调用任何工具。

- 不要使用 read_file、bash、grep、glob、edit_file、write_file 或任何其他工具。
- 上面的对话已经包含了你需要的全部上下文。
- 工具调用会被拒绝并浪费本轮机会，导致任务失败。
- 整个回答必须是纯文本，并包含可审计的投研摘要 <summary> 块。

"""


NO_TOOLS_TRAILER = """
提醒：不要调用任何工具。只能返回纯文本和可审计的投研摘要 <summary> 块。工具调用会被拒绝并导致任务失败。"""


def get_compact_prompt(custom_instructions: str | None = None) -> str:
    """Build the full compaction prompt sent to the model."""
    prompt = NO_TOOLS_PREAMBLE + BASE_COMPACT_PROMPT
    if custom_instructions and custom_instructions.strip():
        prompt += f"\n\n补充说明：\n{custom_instructions}"
    prompt += NO_TOOLS_TRAILER
    return prompt


def format_compact_summary(raw_summary: str) -> str:
    """Strip the <analysis> scratchpad and extract the <summary> content."""
    text = re.sub(r"<analysis>[\s\S]*?</analysis>", "", raw_summary)
    m = re.search(r"<summary>([\s\S]*?)</summary>", text)
    if m:
        text = text.replace(m.group(0), f"摘要：\n{m.group(1).strip()}")
    text = re.sub(r"\n\n+", "\n\n", text)
    return text.strip()


def build_compact_summary_message(
    summary: str,
    *,
    suppress_follow_up: bool = False,
    recent_preserved: bool = False,
) -> str:
    """Create the injected user message that replaces compacted history."""
    formatted = format_compact_summary(summary)
    text = (
        f"本会话将从先前因超出上下文而中断的对话继续。下面的摘要覆盖对话的较早部分。\n\n{formatted}"
    )
    if recent_preserved:
        text += "\n\n最近的消息已逐字保留。"
    if suppress_follow_up:
        text += (
            "\n从中断处直接继续对话，不要再向用户提问。直接恢复任务，不要确认摘要、不要复述进度，"
            "不要以“我会继续”或类似语句开头。把最后一项任务当作从未中断一样继续。"
        )
    return text


# ---------------------------------------------------------------------------
# Auto-compact tracking
# ---------------------------------------------------------------------------


@dataclass
class AutoCompactState:
    """Mutable state that persists across query loop turns."""

    compacted: bool = False
    turn_counter: int = 0
    turn_id: str = ""
    consecutive_failures: int = 0
    last_status: Literal["success", "unchanged", "failed"] = "unchanged"


# ---------------------------------------------------------------------------
# Context window helpers
# ---------------------------------------------------------------------------


def get_autocompact_threshold(
    model: str,
    *,
    context_window_tokens: int | None = None,
    auto_compact_threshold_tokens: int | None = None,
    max_tokens: int = 4096,
) -> int:
    return budget_limits(model, max_tokens, context_window_tokens, auto_compact_threshold_tokens)[3]


def should_autocompact(
    messages: list[ConversationMessage],
    model: str,
    state: AutoCompactState,
    *,
    context_window_tokens: int | None = None,
    auto_compact_threshold_tokens: int | None = None,
    max_tokens: int = 4096,
) -> bool:
    """Return True when the conversation should be auto-compacted."""
    if state.consecutive_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
        return False
    token_count = estimate_message_tokens(messages)
    threshold = get_autocompact_threshold(
        model,
        context_window_tokens=context_window_tokens,
        auto_compact_threshold_tokens=auto_compact_threshold_tokens,
        max_tokens=max_tokens,
    )
    return token_count >= threshold


# ---------------------------------------------------------------------------
# Full compact execution (calls the LLM)
# ---------------------------------------------------------------------------


async def compact_conversation(
    messages: list[ConversationMessage],
    *,
    api_client: Any,
    model: str,
    system_prompt: str = "",
    preserve_recent: int = 6,
    custom_instructions: str | None = None,
    suppress_follow_up: bool = True,
    trigger: CompactTrigger = "manual",
    progress_callback: CompactProgressCallback | None = None,
    emit_hooks_start: bool = True,
    hook_executor: HookExecutor | None = None,
    carryover_metadata: ExecutionMetadata | None = None,
    context_window_tokens: int | None = None,
    max_tokens: int = 4096,
    tools: list[dict[str, Any]] | None = None,
    auto_compact_threshold_tokens: int | None = None,
    snapshot_path: str | None = None,
    runtime_context_provider: Callable[[], str | None | Awaitable[str | None]] | None = None,
    runtime_snapshot_provider: Callable[[], ContextSnapshot | Awaitable[ContextSnapshot]]
    | None = None,
    context_components: ContextComponentsSettings | None = None,
    target_input_tokens: int | None = None,
) -> CompactionResult:
    """Build and validate a candidate without modifying the original history."""
    from researchx.api.client import ApiMessageRequest, ApiMessageCompleteEvent

    original = [m.model_copy(deep=True) for m in messages]
    source_ids: set[str] = set()
    policy_store = (carryover_metadata or {}).get("research_store")
    if policy_store is not None:
        from researchx.state.source_policy import history_use_gap, history_source_ids

        policy_memory = await policy_store.load()
        gap = history_use_gap(policy_memory, original)
        if gap:
            raise ContextBudgetError(gap)
        source_ids = history_source_ids(policy_memory, original)
    before = _conversation_budget(
        original,
        api_client=api_client,
        model=model,
        system_prompt=system_prompt,
        max_tokens=max_tokens,
        tools=tools,
        context_window_tokens=context_window_tokens,
        threshold=auto_compact_threshold_tokens,
    )
    if target_input_tokens is not None:
        before = replace(before, target_tokens=min(before.input_limit, target_input_tokens))
    older, newer = _protected_split(original, preserve_recent)
    if not older:
        return _build_passthrough_compaction_result(
            original,
            trigger=trigger,
            compact_kind="full",
            metadata={"reason": "no completed history available"},
        )
    snapshot_path = snapshot_path or str(
        (await save_context_snapshot(original, model=model, metadata=carryover_metadata))
    )
    metadata = {
        "trigger": trigger,
        "compact_kind": "full",
        "snapshot_path": snapshot_path,
        "pre_compact_message_count": len(original),
        "pre_compact_token_count": before.input_tokens,
    }
    if emit_hooks_start:
        await _emit_progress(
            progress_callback,
            phase="hooks_start",
            trigger=trigger,
            message="Preparing conversation compaction.",
        )
    if hook_executor is not None:
        hook = await hook_executor.execute(
            HookEvent.PRE_COMPACT,
            {
                **(carryover_metadata or {}),
                "event": HookEvent.PRE_COMPACT.value,
                "trigger": trigger,
                "model": model,
                "message_count": len(original),
                "token_count": before.input_tokens,
                "snapshot_path": snapshot_path,
            },
        )
        if hook.blocked:
            return _build_passthrough_compaction_result(
                original,
                trigger=trigger,
                compact_kind="full",
                metadata={"reason": hook.reason or "pre-compact hook blocked", "status": "failed"},
            )
    await _emit_progress(
        progress_callback,
        phase="compact_start",
        trigger=trigger,
        message="Compacting conversation memory.",
        checkpoint="compact_start",
    )
    _validate_tool_pairs(older)
    from researchx.config.context_components import ContextComponentsSettings

    # The summarizer must see the complete archived history to reduce H. It is
    # an internal transformation, so retain its existing global-only safety
    # check; applying the main H cap here would prevent recovery from H overflow.
    summary_policy = (context_components or ContextComponentsSettings()).model_copy(
        update={"enabled": False}
    )
    summary_request = prepare_request(
        api_client,
        ApiMessageRequest(
            model=model,
            messages=_replace_images_with_compaction_placeholders(older)
            + [
                ConversationMessage.from_user_text(
                    get_compact_prompt(custom_instructions),
                    context_origin="continuation",
                    context_component="system_prompt",
                )
            ],
            system_prompt=system_prompt
            or "你是对话摘要助手。请根据用户要求生成准确、可审计的摘要。",
            max_tokens=min(4096, before.window // 10),
            context_window_tokens=before.window,
            current_user_message_id="",
            context_components=summary_policy,
        ),
    )
    # Never discard older rounds to make a summarizer request fit.
    (request_budget(summary_request)).require_fit()

    async def collect() -> str:
        stream = api_client.stream_message(summary_request)
        if inspect.isawaitable(stream):
            stream = await stream
        async for event in stream:
            if isinstance(event, ApiMessageCompleteEvent):
                if (
                    event.stop_reason
                    in {"length", "max_tokens", "incomplete", "error", "cancelled", "tool_use"}
                    or event.message.tool_uses
                    or event.usage.output_tokens >= summary_request.max_tokens
                    or estimate_tokens(event.message.text, model) > summary_request.max_tokens
                ):
                    raise ContextBudgetError("摘要未完整生成；原始内容已保留")
                match = re.search(r"<summary>([\s\S]*?)</summary>", event.message.text)
                if match and match.group(1).strip():
                    return match.group(0)
        raise RuntimeError(ERROR_MESSAGE_INCOMPLETE_RESPONSE)

    for attempt in range(MAX_COMPACT_STREAMING_RETRIES + 1):
        try:
            summary = await asyncio.wait_for((collect()), COMPACT_TIMEOUT_SECONDS)
            break
        except Exception as exc:
            if (
                isinstance(exc, ContextBudgetError)
                or _is_prompt_too_long_error(exc)
                or attempt == MAX_COMPACT_STREAMING_RETRIES
            ):
                raise
            await _emit_progress(
                progress_callback,
                phase="compact_retry",
                trigger=trigger,
                attempt=attempt + 1,
                message=str(exc),
            )
    (await _validate_summary_ids(summary, original, carryover_metadata))
    hook_attachments = []
    if hook_executor is not None:
        hook = await hook_executor.execute(
            HookEvent.POST_COMPACT,
            {
                **(carryover_metadata or {}),
                "event": HookEvent.POST_COMPACT.value,
                "trigger": trigger,
                "model": model,
                "snapshot_path": snapshot_path,
                "pre_compact_tokens": before.input_tokens,
            },
        )
        if hook.blocked:
            raise ContextBudgetError(hook.reason or "post-compact hook blocked")
        hook_attachments = _create_hook_attachments(
            hook.reason or "\n".join(r.output.strip() for r in hook.results if r.output.strip())
        )
    result = CompactionResult(
        trigger=trigger,
        compact_kind="full",
        boundary_marker=create_compact_boundary_message(metadata),
        summary_messages=[
            ConversationMessage.from_user_text(
                (
                    build_compact_summary_message(
                        summary, suppress_follow_up=suppress_follow_up, recent_preserved=bool(newer)
                    )
                ),
                context_origin="compaction",
            )
        ],
        messages_to_keep=newer,
        attachments=(
            await _build_compact_attachments(older, metadata=carryover_metadata, model=model)
        ),
        hook_results=hook_attachments,
        compact_metadata=metadata,
    )
    for summary_message in result.summary_messages:
        summary_message.research_citations = {"source_ids": sorted(source_ids)}
    candidate = build_post_compact_messages(result)
    (await _refresh_runtime(candidate, runtime_context_provider, runtime_snapshot_provider))
    # Keep any refreshed runtime item in the exact candidate we validate and return.
    result.runtime_messages = candidate[len((build_post_compact_messages(result))) :]
    candidate = build_post_compact_messages(result)
    _validate_tool_pairs(candidate)
    after = _conversation_budget(
        candidate,
        api_client=api_client,
        model=model,
        system_prompt=system_prompt,
        max_tokens=max_tokens,
        tools=tools,
        context_window_tokens=before.window,
        threshold=auto_compact_threshold_tokens,
    )
    if after.input_tokens > before.target_tokens or after.input_tokens >= before.input_tokens:
        raise ContextBudgetError("压缩候选未达到目标预算；原始内容已保留")
    result.compact_metadata.update(
        post_compact_message_count=len(candidate), post_compact_token_count=after.input_tokens
    )
    # Metadata is diagnostic only: do not change the already-validated boundary text.
    await _emit_progress(
        progress_callback,
        phase="compact_end",
        trigger=trigger,
        message="对话压缩完成。",
        checkpoint="compact_end",
        metadata=_record_compact_checkpoint(
            carryover_metadata,
            checkpoint="compact_end",
            trigger=trigger,
            message_count=len(candidate),
            token_count=after.input_tokens,
            details={
                "snapshot_path": snapshot_path,
                "tokens_saved": before.input_tokens - after.input_tokens,
            },
        ),
    )
    return result


def _conversation_budget(
    messages: list[ConversationMessage],
    *,
    api_client: SupportsStreamingMessages,
    model: str,
    system_prompt: str = "",
    max_tokens: int = 4096,
    tools: list[dict[str, Any]] | None = None,
    context_window_tokens: int | None = None,
    threshold: int | None = None,
    context_components: ContextComponentsSettings | None = None,
    current_user_message_id: str | None = None,
) -> RequestBudget:
    from researchx.api.client import ApiMessageRequest

    request = prepare_request(
        api_client,
        ApiMessageRequest(
            model=model,
            messages=messages,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            tools=tools or [],
            context_window_tokens=context_window_tokens,
            context_components=context_components,
            current_user_message_id=current_user_message_id,
        ),
    )
    return request_budget(request, threshold=threshold)


def _latest_user_index(messages: list[ConversationMessage]) -> int:
    from researchx.services.context.sources import latest_user_index

    index, _ = latest_user_index(messages)
    return index if index is not None else len(messages)


def _protected_split(
    messages: list[ConversationMessage], preserve_recent: int
) -> tuple[list[ConversationMessage], list[ConversationMessage]]:
    index = min(max(0, len(messages) - max(1, preserve_recent)), _latest_user_index(messages))
    uses = {
        b.id: i for i, m in enumerate(messages) for b in m.content if isinstance(b, ToolUseBlock)
    }
    while True:
        crossings = [
            uses[b.tool_use_id]
            for i, m in enumerate(messages)
            if i >= index
            for b in m.content
            if isinstance(b, ToolResultBlock)
            and b.tool_use_id in uses
            and uses[b.tool_use_id] < index
        ]
        if not crossings:
            break
        index = min(crossings)
    return messages[:index], messages[index:]


def _validate_tool_pairs(messages: list[ConversationMessage]) -> None:
    pending = set()
    for message in messages:
        for block in message.content:
            if isinstance(block, ToolUseBlock):
                if block.id in pending:
                    raise ContextBudgetError("Duplicate tool call in compact candidate")
                pending.add(block.id)
            elif isinstance(block, ToolResultBlock):
                if block.tool_use_id not in pending:
                    raise ContextBudgetError("Unpaired tool result in compact candidate")
                pending.remove(block.tool_use_id)
    if pending:
        raise ContextBudgetError(
            "Unfinished tool calls must be preserved, not discarded during compaction"
        )


async def _validate_summary_ids(
    summary: str, messages: list[ConversationMessage], metadata: ExecutionMetadata | None
) -> None:
    pattern = r"\bev_[A-Za-z0-9_]+\b"
    store = (metadata or {}).get("research_store")
    if store is not None:
        known = set((await store.load()).evidence_pool)
    else:
        known = set(re.findall(pattern, "\n".join(m.model_dump_json() for m in messages)))
    references = set(re.findall(pattern, summary))
    marked = set(re.findall(r"\[E:([^\]\s]+)\]", summary))
    if (references | marked) - known:
        raise ContextBudgetError("摘要包含无效证据 ID；原始内容已保留")


async def _refresh_runtime(
    messages: list[ConversationMessage],
    provider: Callable[[], str | None | Awaitable[str | None]] | None,
    snapshot_provider: Callable[[], ContextSnapshot | Awaitable[ContextSnapshot]] | None = None,
) -> None:
    if snapshot_provider is not None:
        snapshot = snapshot_provider()
        if inspect.isawaitable(snapshot):
            snapshot = await snapshot
        messages[:] = refresh_runtime_messages(messages, snapshot)
        return
    if provider is not None:
        current = provider()
        if inspect.isawaitable(current):
            current = await current
        previous = next((m.runtime_context for m in reversed(messages) if m.runtime_context), None)
        if current and current != previous:
            messages.append(
                ConversationMessage(role="user", context_origin="runtime", runtime_context=current)
            )


async def auto_compact_if_needed(
    messages: list[ConversationMessage],
    *,
    api_client: Any,
    model: str,
    system_prompt: str = "",
    state: AutoCompactState,
    preserve_recent: int = 6,
    progress_callback: CompactProgressCallback | None = None,
    force: bool = False,
    trigger: CompactTrigger = "auto",
    hook_executor: HookExecutor | None = None,
    carryover_metadata: ExecutionMetadata | None = None,
    context_window_tokens: int | None = None,
    auto_compact_threshold_tokens: int | None = None,
    max_tokens: int = 4096,
    tools: list[dict[str, Any]] | None = None,
    runtime_context_provider: Callable[[], str | None | Awaitable[str | None]] | None = None,
    history_target_tokens: int | None = None,
    runtime_snapshot_provider: Callable[[], ContextSnapshot | Awaitable[ContextSnapshot]]
    | None = None,
    context_components: ContextComponentsSettings | None = None,
    current_user_message_id: str | None = None,
) -> tuple[list[ConversationMessage], bool]:
    kwargs = dict(
        api_client=api_client,
        model=model,
        system_prompt=system_prompt,
        max_tokens=max_tokens,
        tools=tools,
        context_window_tokens=context_window_tokens,
        threshold=auto_compact_threshold_tokens,
        context_components=context_components,
        current_user_message_id=current_user_message_id,
    )
    before = _conversation_budget(messages, **kwargs)
    history_over = (
        history_target_tokens is not None
        and before.component_tokens["conversation_history"] > history_target_tokens
    )
    if (
        history_over
        and history_target_tokens is not None
        and before.input_tokens < before.trigger_tokens
    ):
        # Existing compaction still accepts only a smaller, globally safe candidate.
        # H is an extra trigger; do not use force=True to bypass failure backoff.
        before = replace(
            before,
            target_tokens=min(
                before.input_limit,
                before.input_tokens
                - before.component_tokens["conversation_history"]
                + history_target_tokens,
            ),
        )
    state.last_status = "unchanged"
    if not force and (
        state.consecutive_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES
        or before.input_tokens < before.trigger_tokens
        and not history_over
    ):
        return messages, False
    _record_compact_checkpoint(
        carryover_metadata,
        checkpoint=f"query_{trigger}_triggered",
        trigger=trigger,
        message_count=len(messages),
        token_count=before.input_tokens,
    )
    snapshot = None
    try:
        snapshot = str(
            (await save_context_snapshot(messages, model=model, metadata=carryover_metadata))
        )
        _record_compact_checkpoint(
            carryover_metadata,
            checkpoint="compact_snapshot_saved",
            trigger=trigger,
            message_count=len(messages),
            token_count=before.input_tokens,
            details={"snapshot_path": snapshot},
        )
        candidate, saved = await microcompact_messages(messages, snapshot_path=snapshot)
        (await _refresh_runtime(candidate, runtime_context_provider, runtime_snapshot_provider))
        after = _conversation_budget(candidate, **kwargs)
        _record_compact_checkpoint(
            carryover_metadata,
            checkpoint="query_microcompact_end",
            trigger=trigger,
            message_count=len(candidate),
            token_count=after.input_tokens,
            details={"snapshot_path": snapshot, "tokens_freed": saved},
        )
        if (
            saved > 0
            and after.input_tokens <= before.target_tokens
            and after.input_tokens < before.input_tokens
            and after.fits
            and after.components_fit
        ):
            _validate_tool_pairs(candidate)
            _record_compact_checkpoint(
                carryover_metadata,
                checkpoint="compact_end",
                trigger=trigger,
                message_count=len(candidate),
                token_count=after.input_tokens,
                details={"snapshot_path": snapshot},
            )
        else:
            # Deliberately use ORIGINAL messages, not cleared results, for summarization.
            result = await compact_conversation(
                messages,
                api_client=api_client,
                model=model,
                system_prompt=system_prompt,
                preserve_recent=preserve_recent,
                trigger=trigger,
                progress_callback=progress_callback,
                hook_executor=hook_executor,
                carryover_metadata=carryover_metadata,
                context_window_tokens=context_window_tokens,
                max_tokens=max_tokens,
                tools=tools,
                auto_compact_threshold_tokens=auto_compact_threshold_tokens,
                snapshot_path=snapshot,
                runtime_context_provider=runtime_context_provider,
                runtime_snapshot_provider=runtime_snapshot_provider,
                context_components=context_components,
                target_input_tokens=before.target_tokens,
            )
            if result.status != "success":
                state.last_status = result.status
                state.consecutive_failures += 1
                return messages, False
            candidate = build_post_compact_messages(result)
        final_budget = _conversation_budget(candidate, **kwargs)
        final_budget.require_fit()
        state.compacted = True
        state.turn_counter += 1
        state.turn_id = uuid4().hex
        state.consecutive_failures = 0
        state.last_status = "success"
        return candidate, True
    except Exception as exc:
        state.consecutive_failures += 1
        state.last_status = "failed"
        checkpoint = _record_compact_checkpoint(
            carryover_metadata,
            checkpoint=f"query_{trigger}_failed",
            trigger=trigger,
            message_count=len(messages),
            token_count=before.input_tokens,
            details={"reason": str(exc), "snapshot_path": snapshot},
        )
        await _emit_progress(
            progress_callback,
            phase="compact_failed",
            trigger=trigger,
            message=str(exc),
            metadata=checkpoint,
        )
        log.warning("Compaction rejected; original history retained: %s", exc)
        return messages, False


# ---------------------------------------------------------------------------
# Legacy compat
# ---------------------------------------------------------------------------


def summarize_messages(
    messages: list[ConversationMessage],
    *,
    max_messages: int = 8,
) -> str:
    """Produce a compact textual summary of recent messages (legacy)."""
    selected = messages[-max_messages:]
    lines: list[str] = []
    for message in selected:
        text = message.text.strip()
        if not text:
            continue
        lines.append(f"{message.role}: {text[:300]}")
    return "\n".join(lines)


def compact_messages(
    messages: list[ConversationMessage],
    *,
    preserve_recent: int = 6,
) -> list[ConversationMessage]:
    """Replace older conversation history with a synthetic summary (legacy)."""
    if len(messages) <= preserve_recent:
        return sanitize_conversation_messages((list(messages)))
    older, newer = _split_preserving_tool_pairs(messages, preserve_recent=preserve_recent)
    summary = summarize_messages(older)
    if not summary:
        return list(newer)
    return sanitize_conversation_messages(
        [
            ConversationMessage(
                role="user",
                content=[TextBlock(text=f"[conversation summary]\n{summary}")],
            ),
            *newer,
        ]
    )


__all__ = [
    "AutoCompactState",
    "CompactAttachment",
    "CompactionResult",
    "COMPACTABLE_TOOLS",
    "TIME_BASED_MC_CLEARED_MESSAGE",
    "auto_compact_if_needed",
    "build_post_compact_messages",
    "build_compact_summary_message",
    "compact_conversation",
    "compact_messages",
    "create_compact_boundary_message",
    "estimate_conversation_tokens",
    "estimate_message_tokens",
    "format_compact_summary",
    "get_autocompact_threshold",
    "get_compact_prompt",
    "microcompact_messages",
    "should_autocompact",
    "summarize_messages",
]
