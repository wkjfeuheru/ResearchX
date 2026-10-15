"""Deterministic company-event-monitor processing."""

from __future__ import annotations

# Direct file execution resolves the same package as an installed module.
if not __package__:
    from researchx.skills.runtime_support import script_package

    __package__ = script_package(__file__)

from datetime import timedelta
from zoneinfo import ZoneInfo
from .models import MonitorResult
from researchx.skills.runtime_support import processing_main

SHANGHAI = ZoneInfo("Asia/Shanghai")


def normalize_monitor(result: MonitorResult) -> MonitorResult:
    result.as_of = result.as_of.astimezone(SHANGHAI)
    start = result.window_start or result.as_of - timedelta(days=7)
    if start.tzinfo is None or start > result.as_of:
        raise ValueError("检索窗口须带时区且不晚于截止时间")
    result.window_start = start.astimezone(SHANGHAI)
    merged = {}
    for event in [*result.events, *result.undated_events, *result.outside_window]:
        if event.event_key not in merged:
            merged[event.event_key] = event.model_copy(deep=True)
            continue
        previous = merged[event.event_key]
        if event.summary != previous.summary:
            previous.summary += "\n其他来源表述：" + event.summary
        for reference in event.references:
            if reference not in previous.references:
                previous.references.append(reference)
        # Do not erase conflicting coverage under the guise of deduplication.
        for key in ("sentiment", "impact"):
            old, new = getattr(previous, key), getattr(event, key)
            if old.value != new.value:
                old.value = None
                old.reason = "同事件来源判断冲突：" + old.reason + "；" + new.reason
                old.confidence = "low"
                result.gaps.append(f"{event.title}: {key}判断冲突")
        if previous.published_at is not None and event.published_at is not None:
            # Reprints do not make an old event become a new announcement.
            previous.published_at = min(previous.published_at, event.published_at)
        elif previous.published_at is None:
            previous.published_at = event.published_at
        previous.summary_only = previous.summary_only and event.summary_only
    result.events, result.undated_events, result.outside_window = [], [], []
    for event in merged.values():
        if event.summary_only:
            result.gaps.append(f"{event.title}: 仅取得摘要")
        if event.impact.value is None:
            result.gaps.append(f"{event.title}: 基本面影响证据不足/冲突")
        if event.published_at is None:
            result.undated_events.append(event)
        elif not start <= event.published_at <= result.as_of:
            result.outside_window.append(event)
        else:
            result.events.append(event)
    result.events.sort(
        key=lambda item: item.occurred_at or item.published_at or result.as_of, reverse=True
    )
    result.outside_window.sort(key=lambda item: item.published_at or result.as_of, reverse=True)
    for channel in result.channels:
        if channel.state != "ok":
            result.gaps.append(f"{channel.name}: {channel.state} {channel.detail}")
    if result.undated_events:
        result.gaps.append("存在发布日期未知的资料，单列且不认定在近期窗口内")
    if result.outside_window:
        result.gaps.append("窗口外资料已单列，不计入近期事件")
    result.gaps = list(dict.fromkeys(result.gaps))
    usable = any(channel.state in {"ok", "summary_only"} for channel in result.channels)
    result.status = "blocked" if not usable else "partial" if result.gaps else "complete"
    return result


def main(argv: list[str] | None = None) -> None:
    processing_main(MonitorResult, normalize_monitor, argv)


if __name__ == "__main__":
    main()
