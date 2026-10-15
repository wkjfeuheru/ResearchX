"""Compact provenance manifests; text is stored/sent once, offsets are private."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import TYPE_CHECKING
from researchx.config.context_components import ContextComponent, ContextComponentsSettings

if TYPE_CHECKING:
    from researchx.state.store import ResearchStore
    from researchx.state.runtime import ResearchAgentRuntime
from researchx.engine.messages import (
    ContextSpan,
    ConversationMessage,
    ImageBlock,
    TextBlock,
    ToolResultBlock,
)

MEMORY_KEYS = {
    "items",
    "conflicts",
    "arbitrations",
    "conclusions",
    "evidence_pool",
    "sources",
    "reasoning_chain",
}


@dataclass(frozen=True)
class ContextSnapshot:
    text: str = ""
    manifest: list[ContextSpan] = field(default_factory=list)

    @classmethod
    def join(cls, pieces: list[tuple[str, ContextComponent, str, bool]]) -> ContextSnapshot:
        text, spans = "", []
        for body, component, source, deferrable in pieces:
            if not body:
                continue
            separator = "\n\n" if text else ""
            start = len(text)
            text += separator + body
            spans.append(
                ContextSpan(
                    start=start,
                    end=len(text),
                    component=component,
                    source=source,
                    deferrable=deferrable,
                )
            )
        return cls(text, spans)


def tagged_snapshot(text: str, source: str) -> ContextSnapshot:
    """Classify a producer's known JSON packet using exact raw character offsets."""
    spans, cursor = [], 0
    decoder = json.JSONDecoder()
    start = text.index("{") + 1
    while True:
        start = next((i for i in range(start, len(text)) if not text[i].isspace()), len(text))
        if text[start] == "}":
            break
        key, end = decoder.raw_decode(text, start)
        start = end
        while text[start].isspace() or text[start] == ":":
            start += 1
        value, end = decoder.raw_decode(text, start)
        if start > cursor:
            spans.append(
                ContextSpan(start=cursor, end=start, component="dynamic_context", source=source)
            )
        memory = bool(value) and (
            key in MEMORY_KEYS if source == "research_store" else key == "content"
        )
        spans.append(
            ContextSpan(
                start=start,
                end=end,
                component="memory" if memory else "dynamic_context",
                source=source,
            )
        )
        cursor, start = end, end
        while text[start].isspace():
            start += 1
        if text[start] == ",":
            start += 1
        elif text[start] == "}":
            break
        else:
            raise ValueError("Invalid context packet")
    spans.append(
        ContextSpan(start=cursor, end=len(text), component="dynamic_context", source=source)
    )
    return ContextSnapshot(text, spans)


def runtime_fragments(
    text: str, manifest: list[ContextSpan] | None
) -> tuple[list[tuple[str, ContextComponent, bool]], int]:
    """Use validated offsets; legacy owned delimiters get a structured fallback.

    Unclassified text is D, once. Only known tagged JSON structures are treated
    as memory; arbitrary prose is never searched for semantic keywords.
    """
    result: list[tuple[str, ContextComponent, bool]]
    if not text:
        return [], 0
    if manifest:
        try:
            spans = sorted(
                (ContextSpan.model_validate(span) for span in manifest), key=lambda span: span.start
            )
        except (ValueError, TypeError):
            spans = []
        if spans and all(
            0 <= span.start <= span.end <= len(text) and (i == 0 or spans[i - 1].end <= span.start)
            for i, span in enumerate(spans)
        ):
            result, cursor, fallback = [], 0, 0
            for span in spans:
                if span.start > cursor:
                    result.append((text[cursor : span.start], "dynamic_context", False))
                    fallback += 1
                result.append((text[span.start : span.end], span.component, span.deferrable))
                cursor = span.end
            if cursor < len(text):
                result.append((text[cursor:], "dynamic_context", False))
                fallback += 1
            return result, fallback
    result, cursor, fallback = [], 0, 0
    for match in re.finditer(
        r"<(research_memory|workspace_memory|long_term_memory)>(.*?)</\1>", text, re.S
    ):
        if match.start() > cursor:
            result.append((text[cursor : match.start()], "dynamic_context", False))
            fallback += 1
        body = match.group(2).strip()
        try:
            # Workspace snapshots have a fixed preamble before their JSON packet.
            value = json.loads(body[body.index("{") :])
            if not isinstance(value, dict):
                raise ValueError("not a mapping")
            if match.group(1) == "research_memory":
                snapshot = tagged_snapshot(match.group(0), "research_store")
                result.extend(
                    (snapshot.text[span.start : span.end], span.component, False)
                    for span in snapshot.manifest
                )
            elif match.group(1) == "workspace_memory" and "content" in value:
                snapshot = tagged_snapshot(match.group(0), "workspace_memory")
                result.extend(
                    (snapshot.text[span.start : span.end], span.component, False)
                    for span in snapshot.manifest
                )
            else:
                result.append((match.group(0), "dynamic_context", False))
        except (ValueError, KeyError, IndexError):
            result.append((match.group(0), "dynamic_context", False))
        fallback += 1
        cursor = match.end()
    if cursor < len(text):
        result.append((text[cursor:], "dynamic_context", False))
        fallback += 1
    return result, fallback


def latest_user_index(
    messages: list[ConversationMessage], message_id: str | None = None
) -> tuple[int | None, int]:
    def real(message: ConversationMessage) -> bool:
        return (
            message.role == "user"
            and message.context_origin not in {"runtime", "continuation", "compaction"}
            and any(
                isinstance(block, ImageBlock)
                or isinstance(block, TextBlock)
                and (
                    message.context_origin == "user_input"
                    or block.context_component in {None, "user_message"}
                )
                for block in message.content
            )
            and not any(isinstance(block, ToolResultBlock) for block in message.content)
        )

    if message_id is not None:
        return next(
            (
                index
                for index in reversed(range(len(messages)))
                if messages[index].message_id == message_id and real(messages[index])
            ),
            None,
        ), 0
    candidates = [index for index, message in enumerate(messages) if real(message)]
    if not candidates:
        return None, 0
    latest = candidates[-1]
    return latest, int(messages[latest].context_origin != "user_input")


def combine_snapshots(snapshots: list[ContextSnapshot]) -> ContextSnapshot:
    text, spans = "", []
    for snapshot in snapshots:
        if not snapshot.text:
            continue
        if text:
            start = len(text)
            text += "\n\n"
            spans.append(ContextSpan(start=start, end=len(text), component="dynamic_context"))
        offset = len(text)
        text += snapshot.text
        spans.extend(
            span.model_copy(update={"start": span.start + offset, "end": span.end + offset})
            for span in snapshot.manifest
        )
    return ContextSnapshot(text, spans)


async def compose_research_context(
    base: ContextSnapshot,
    *,
    store: ResearchStore | None = None,
    runtime: ResearchAgentRuntime | None = None,
    model: str = "",
    output_tokens: int = 4096,
    window: int | None = None,
    policy: ContextComponentsSettings | None = None,
    legacy_budget: int = 6000,
    enabled: bool = True,
    auto_summary_max_tokens: int = 1500,
    auto_index_max_items: int = 12,
) -> ContextSnapshot:
    from researchx.services.context.budget import budget_limits
    from researchx.services.context.token_estimation import estimate_tokens

    if not enabled or store is None:
        return base
    policy = policy or ContextComponentsSettings()
    _, _, available, _, _ = budget_limits(model, output_tokens, window)
    targets, maxima = policy.limits(available)
    quota = (
        min(legacy_budget, targets["memory"], maxima["memory"]) if policy.enabled else legacy_budget
    )
    from researchx.state.episodes import EpisodicRepository

    summary = await EpisodicRepository(store.workspace_id, store.session_id).context_summary(
        max_items=auto_index_max_items
    )
    summary["memory_path"] = "MEMORY.md"
    summary["notice"] = "详细记录使用 research_memory.search/read；研究笔记使用 read_file。"
    budget = max(0, min(legacy_budget, quota, auto_summary_max_tokens))

    def packet() -> str:
        return "<research_memory>\n" + json.dumps(summary, ensure_ascii=False).replace(
            "<", "\\u003c"
        ).replace(">", "\\u003e") + "\n</research_memory>"

    items = summary["items"]
    assert isinstance(items, list)
    while items and estimate_tokens(packet(), model) > budget:
        items.pop()
    if estimate_tokens(packet(), model) > budget:
        summary["unresolved"] = []
        summary["next_step"] = ""
    if estimate_tokens(packet(), model) > budget:
        return base
    return combine_snapshots([base, tagged_snapshot(packet(), "research_store")])


def refresh_runtime_messages(
    messages: list[ConversationMessage], snapshot: ContextSnapshot, *, strip_memory: bool = False
) -> list[ConversationMessage]:
    """Replace owned mutable recall on copies; preserve ordinary cached prefixes."""
    if strip_memory and snapshot.text:
        # A legacy callback may still return a stale recall when memory is off.
        # Sanitize that new packet through the same range-preserving removal;
        # the empty snapshot ends this one-level normalization immediately.
        cleaned = refresh_runtime_messages(
            [
                ConversationMessage(
                    role="user",
                    runtime_context=snapshot.text,
                    runtime_context_manifest=snapshot.manifest or None,
                )
            ],
            ContextSnapshot(),
            strip_memory=True,
        )
        snapshot = (
            ContextSnapshot(
                cleaned[0].runtime_context or "", cleaned[0].runtime_context_manifest or []
            )
            if cleaned
            else ContextSnapshot()
        )
    if not snapshot.text and not strip_memory:
        return list(messages)
    latest = next((i for i in reversed(range(len(messages))) if messages[i].runtime_context), None)
    same = latest is not None and messages[latest].runtime_context == snapshot.text
    managed = strip_memory or any(
        span.source in {"research_store", "workspace_memory"} for span in snapshot.manifest
    )
    result = []
    for index, message in enumerate(messages):
        if same and index == latest and snapshot.manifest:
            message = message.model_copy(update={"runtime_context_manifest": snapshot.manifest})
        if managed and message.runtime_context and not (same and index == latest):
            # Only our explicit recall envelopes, never user/tool text or arbitrary prose.
            text = re.sub(
                r"\n*<(research_memory|workspace_memory|long_term_memory)>.*?</\1>",
                "",
                message.runtime_context,
                flags=re.S,
            )
            if text != message.runtime_context:
                fragments, fallback = runtime_fragments(
                    message.runtime_context, message.runtime_context_manifest
                )
                spans = []
                if not fallback:
                    # Preserve S rules when an older memory envelope is removed.
                    text, offset = "", 0
                    removed = list(
                        re.finditer(
                            r"\n*<(research_memory|workspace_memory|long_term_memory)>.*?</\1>",
                            message.runtime_context,
                            flags=re.S,
                        )
                    )
                    for body, component, optional in fragments:
                        end, cursor, kept = offset + len(body), offset, []
                        for match in removed:
                            if match.end() <= cursor or match.start() >= end:
                                continue
                            kept.append(message.runtime_context[cursor : min(end, match.start())])
                            cursor = min(end, match.end())
                        kept.append(message.runtime_context[cursor:end])
                        offset, body = end, "".join(kept)
                        if not body:
                            continue
                        start = len(text)
                        text += body
                        spans.append(
                            ContextSpan(
                                start=start, end=len(text), component=component, deferrable=optional
                            )
                        )
                else:
                    spans = (
                        [ContextSpan(start=0, end=len(text), component="dynamic_context")]
                        if text
                        else []
                    )
                message = message.model_copy(
                    update={
                        "runtime_context": text or None,
                        "runtime_context_manifest": spans or None,
                    }
                )
        if message.content or message.runtime_context:
            result.append(message)
    if snapshot.text and not same:
        result.append(
            ConversationMessage(
                role="user",
                context_origin="runtime",
                runtime_context=snapshot.text,
                runtime_context_manifest=snapshot.manifest or None,
            )
        )
    return result
