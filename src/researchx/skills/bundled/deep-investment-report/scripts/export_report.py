"""Skill-owned report organization and export configuration for deep-investment-report."""

from __future__ import annotations
from typing import Any

from pathlib import Path

# Direct file execution resolves the same package as an installed module.
if not __package__:
    from researchx.skills.runtime_support import script_package

    __package__ = script_package(__file__)

from .models import DeepResult
from researchx.workspace.exports import (
    ReportContext,
    display,
    export_result as write_exports,
)
from researchx.skills.runtime_support import export_main

SHEETS = ("company", "base_revenue", "scenarios", "forecasts", "sensitivity", "gaps", "inputs")


def render_markdown(data: dict[str, Any], session_directory: Path | None = None) -> str:
    context = ReportContext(data, session_directory)
    refs, claim = context.refs, context.claim
    lines = context.header("深度投研报告")
    for section in data["sections"]:
        lines.append(f"## {section['title']}")
        lines.extend(claim(item) for item in section["paragraphs"])
        lines.extend("缺口：" + item for item in section["gaps"])
    lines.extend(
        ["## 三年情景盈利预测", "预测基于显式假设，非已实现业绩；未生成自主评级或目标价。"]
    )
    baseline = data["base_revenue"]
    lines.append(
        f"基期：{data['base_year']}年度合并收入 {display(baseline['value'])} {baseline['unit']} {data['base_currency']}；{refs(baseline['references'])}"
    )
    lines.append(
        "| 情景 | 年度 | 收入（元） | 营业利润（元） | 税前利润（元） | 合并净利润（元） | 归母净利润（元） | EPS |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for row in data["forecasts"]:
        values = row["values"]
        lines.append(
            f"| {row['scenario']} | {row['year']} | {values['revenue']} | {values['operating_profit']} | {values['pretax_profit']} | {values['net_profit']} | {values['parent_net_profit']} | {display(values['eps'])} |"
        )
    lines.append("### 预测假设与依据")
    for scenario in data["scenarios"]:
        for year in scenario["years"]:
            for key, assumption in year["assumptions"].items():
                lines.append(
                    f"- {scenario['name']}/{year['year']} {key}: {display(assumption['value'])} ({assumption['origin']})；{assumption['rationale']} {refs(assumption['references'])}"
                )
    lines.append("### 单年度敏感性（其他条件保持基准）")
    lines.extend(
        f"- {row['year']}: 增速变动 {row['growth_shift']}，毛利率变动 {row['gross_margin_shift']}，归母净利润 {row['parent_net_profit']} 元"
        for row in data["sensitivity"]
    )
    return context.finish(lines)


def export_result(
    result: DeepResult,
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
    export_main(DeepResult, export_result, argv)


if __name__ == "__main__":
    main()
