"""Skill-owned report organization and export configuration for financial-statement-analysis."""

from __future__ import annotations
from typing import Any

from pathlib import Path

# Direct file execution resolves the same package as an installed module.
if not __package__:
    from researchx.skills.runtime_support import script_package

    __package__ = script_package(__file__)

from .models import FinancialResult
from researchx.workspace.exports import (
    ReportContext,
    display,
    export_result as write_exports,
)
from researchx.skills.runtime_support import export_main
from .analyze_statements import CORE_ITEMS

METRIC_LABELS = {
    "gross_margin": "毛利率",
    "net_margin": "合并净利率",
    "debt_ratio": "资产负债率",
    "current_ratio": "流动比率",
    "cash_to_profit": "经营现金流/合并净利润",
    "simple_roe": "简化ROE（非加权平均）",
}

SHEETS = ("company", "periods", "ratios", "checks", "changes", "notes", "gaps", "inputs")


def render_markdown(data: dict[str, Any], session_directory: Path | None = None) -> str:
    context = ReportContext(data, session_directory)
    refs = context.refs
    lines = context.header("财报穿透解析")
    for period in data["periods"]:
        lines.extend(
            [
                f"## {period['label']} / {period['scope']} / {period['currency']}",
                f"期间：{period['start']} 至 {period['end']}；重述：{period['restated']}",
                "| 科目 | 原披露数值 | 单位 | 依据/缺口 |",
                "|---|---:|---|---|",
            ]
        )
        for key, item in period["items"].items():
            lines.append(
                f"| {CORE_ITEMS[key]} | {display(item['value'])} | {item['unit']} | {refs(item['references'])} {item['missing_reason']} |"
            )
    lines.extend(
        [
            "## 财务比率",
            "| 期间/口径 | 指标 | 比率值（未年化） | 公式 | 限制 |",
            "|---|---|---:|---|---|",
        ]
    )
    for row in data["ratios"]:
        lines.append(
            f"| {row['period']}/{row['scope']} | {METRIC_LABELS[row['metric']]} | {display(row['value'])} | {row['formula']} | {row['limitation']} {refs(row['references'])} |"
        )
    lines.append("## 勾稽校验")
    for row in data["checks"]:
        lines.append(
            f"- {row['period']}/{row['scope']} {row['check']}: {row['state']}；差额 {display(row['residual_yuan'])} 元，容差 {row['tolerance_yuan']} 元。{refs(row['references'])}"
        )
    lines.append("## 同比与亏损方向")
    for row in data["changes"]:
        lines.append(
            f"- {row['period']}/{row['scope']} {CORE_ITEMS[row['metric']]}：{row['direction']}，差额 {row['difference_yuan']} 元；同比 {display(row['yoy'])}。{row['limitation']} {refs(row['references'])}"
        )
    lines.append("## 关键附注核查")
    for finding in data["notes"]:
        lines.extend(
            [
                f"### {finding['topic']}",
                f"事实：{finding['fact']} {refs(finding['references'])}",
                f"解释：{finding['interpretation']}",
            ]
        )
    return context.finish(lines)


def export_result(
    result: FinancialResult,
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
    export_main(FinancialResult, export_result, argv)


if __name__ == "__main__":
    main()
