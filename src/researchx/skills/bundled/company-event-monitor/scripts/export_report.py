"""Skill-owned report organization and export configuration for company-event-monitor."""

from __future__ import annotations
from typing import Any

from pathlib import Path

# Direct file execution resolves the same package as an installed module.
if not __package__:
    from researchx.skills.runtime_support import script_package

    __package__ = script_package(__file__)

from .models import MonitorResult
from researchx.workspace.exports import (
    ReportContext,
    display,
    export_result as write_exports,
)
from researchx.skills.runtime_support import export_main

SHEETS = ("company", "channels", "events", "undated_events", "outside_window", "gaps", "inputs")


def render_markdown(data: dict[str, Any], session_directory: Path | None = None) -> str:
    context = ReportContext(data, session_directory)
    refs = context.refs
    lines = context.header("舆情与公告监控")
    lines.append(f"检索窗口：{data['window_start']} 至 {data['as_of']}；覆盖仅限实际取得资料。")
    for key, heading in (
        ("events", "窗口内事件"),
        ("undated_events", "发布日期未知"),
        ("outside_window", "窗口外资料"),
    ):
        lines.append(f"## {heading}")
        if not data[key]:
            lines.append(
                "未取得此类事件；不代表不存在事件。"
                if data["status"] != "complete"
                else "本次查询未发现此类事件。"
            )
        for event in data[key]:
            lines.extend(
                [
                    f"### {event['title']} / {event['category']}",
                    f"发布：{event['published_at']}；发生：{event['occurred_at']}；仅摘要：{event['summary_only']}",
                    event["summary"] + " " + refs(event["references"]),
                ]
            )
            for score_key, label in (("sentiment", "文本情绪"), ("impact", "基本面影响")):
                score = event[score_key]
                lines.append(
                    f"- {label}：{display(score['value'])}（置信度 {score['confidence']}）；{score['reason']}"
                )
    lines.append("## 渠道状态")
    lines.extend(f"- {row['name']}: {row['state']} {row['detail']}" for row in data["channels"])
    return context.finish(lines)


def export_result(
    result: MonitorResult,
    directory: Path,
    session_directory: Path | None = None,
    task_id: str | None = None,
) -> dict[str, object]:
    return write_exports(
        result,
        directory,
        session_directory,
        task_id,
        render_markdown=render_markdown,
        sheets=SHEETS,
    )


def main(argv: list[str] | None = None) -> None:
    export_main(MonitorResult, export_result, argv)


if __name__ == "__main__":
    main()
