"""Skill-owned report organization and export configuration for research-report-digest."""

from __future__ import annotations
from typing import Any

from pathlib import Path

# Direct file execution resolves the same package as an installed module.
if not __package__:
    from researchx.skills.runtime_support import script_package

    __package__ = script_package(__file__)

from .models import DigestResult
from researchx.workspace.exports import (
    ReportContext,
    display,
    export_result as write_exports,
)
from researchx.skills.runtime_support import export_main

SHEETS = ("company", "reports", "comparison", "gaps", "inputs")


def render_markdown(data: dict[str, Any], session_directory: Path | None = None) -> str:
    context = ReportContext(data, session_directory)
    refs, claim = context.refs, context.claim
    lines = context.header("研报精读与摘要")
    for report in data["reports"]:
        lines.extend(
            [
                f"## {report['title']}",
                f"机构：{report['institution']}；作者：{'、'.join(report['authors'])}；日期：{report['published_at']}；仅摘要：{report['summary_only']}",
                refs(report["references"]),
            ]
        )
        for key, heading in (
            ("views", "核心观点"),
            ("arguments", "核心论据"),
            ("assumptions", "预测假设"),
            ("risks", "风险提示"),
        ):
            lines.append(f"### {heading}")
            lines.extend("- " + claim(item) for item in report[key])
        lines.append("### 原研报盈利预测（非实际业绩）")
        for prediction in report["predictions"]:
            amount = prediction["amount"]
            lines.append(
                f"- {prediction['year']} {prediction['metric']}: {display(amount['value'])} {amount['unit']} {prediction['currency']}；{prediction['basis']} {refs(amount['references'])}"
            )
        rating = report["rating"]
        lines.append(
            f"评级：{rating['current']}；前次：{rating['previous']}；变动：{report['rating_change'] or '未知'} {refs(rating['references'])}"
        )
        target = report["target_price"]
        lines.append(
            f"原研报目标价：{display(target['value'])} {report['target_currency']} {refs(target['references'])}"
            if target
            else "原研报目标价：未知"
        )
    lines.extend(["## 可比预测对照", "以下仅合并同年度、币种、指标和口径，不平均不同机构评级。"])
    for group in data["comparison"]:
        lines.append(
            f"- {group['year']} / {group['metric']} / {group['currency']} / {group['basis']}: "
            + "; ".join(
                f"{item['institution']} ({item['published_at']}) {display(item['value_yuan'])} 元"
                for item in group["predictions"]
            )
        )
    return context.finish(lines)


def export_result(
    result: DigestResult,
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
    export_main(DigestResult, export_result, argv)


if __name__ == "__main__":
    main()
