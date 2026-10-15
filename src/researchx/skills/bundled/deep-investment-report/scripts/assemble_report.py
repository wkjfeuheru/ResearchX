"""Consume and validate upstream forecast results without recalculating them."""

from __future__ import annotations
from typing import Any

if not __package__:
    from researchx.skills.runtime_support import script_package

    __package__ = script_package(__file__)

import argparse
import json
from pathlib import Path
from .models import DeepResult
from .forecast import SECTION_KEYS
from researchx.storage.filesystem import atomic_write_text
from researchx.workspace.exports import collect_references
from researchx.skills.runtime_support import guarded


def assemble_report(
    report: DeepResult, forecast: DeepResult | None, analyses: list[dict[str, Any]]
) -> DeepResult:
    """Preserve upstream values, assumptions, units, dates and source references."""
    keys = [section.key for section in report.sections]
    if len(keys) != len(set(keys)):
        raise ValueError("报告章节不可重复")
    report.gaps.extend(f"缺少报告章节: {key}" for key in sorted(SECTION_KEYS - set(keys)))
    for section in report.sections:
        report.gaps.extend(section.gaps)
        if not section.paragraphs:
            report.gaps.append(f"{section.title}: 缺少有依据的内容")
    if forecast is None:
        if report.forecasts or report.sensitivity:
            raise ValueError("报告预测必须有 earnings-forecast 上游结果")
        report.gaps.append("earnings-forecast 不可用或未形成有效预测")
    else:
        for key in (
            "company",
            "base_year",
            "base_revenue",
            "base_currency",
            "base_scope",
            "as_of",
            "scenarios",
        ):
            if getattr(report, key) != getattr(forecast, key):
                raise ValueError(f"报告 {key} 与预测结果不一致")
        for key in ("forecasts", "sensitivity"):
            if getattr(report, key) and getattr(report, key) != getattr(forecast, key):
                raise ValueError(f"报告 {key} 与上游计算不一致")
            setattr(report, key, getattr(forecast, key))
        report.gaps.extend(forecast.gaps)
        if not forecast.forecasts:
            report.gaps.append("上游未产生有效预测")
    kinds = set()
    all_refs = list(collect_references(forecast.model_dump(mode="json"))) if forecast else []
    for analysis in analyses:
        if analysis.get("company") != report.company.model_dump(mode="json"):
            raise ValueError("前置分析与报告公司身份不一致")
        if analysis.get("as_of") != report.model_dump(mode="json")["as_of"]:
            raise ValueError("前置分析与报告资料截止时间不一致")
        kinds.add(analysis.get("kind"))
        report.gaps.extend(analysis.get("gaps", []))
        all_refs.extend(collect_references(analysis))
        if analysis.get("status") != "complete":
            report.gaps.append(f"{analysis.get('kind')}: 前置分析未完整核验")
    for kind in ("financial", "monitor", "digest"):
        if kind not in kinds:
            report.gaps.append(f"缺少前置分析结果: {kind}")
    for section in report.sections:
        for claim in section.paragraphs:
            if claim.kind == "unclassified":
                report.gaps.append(f"{section.title}: 事实、推断或预测未分类")
            for ref in claim.references:
                # Keep real references, never invent evidence identifiers.
                if ref.model_dump(mode="json") not in all_refs:
                    report.gaps.append(f"{section.title}: 引用未在上游材料中核验: {ref.locator}")
    report.gaps = list(dict.fromkeys(report.gaps))
    report.status = "partial" if report.gaps else "complete"
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--forecast")
    parser.add_argument("--analysis", action="append", default=[])
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    def process() -> None:
        report = DeepResult.model_validate_json(Path(args.input).read_text())
        forecast = (
            DeepResult.model_validate_json(Path(args.forecast).read_text())
            if args.forecast
            else None
        )
        analyses = [json.loads(Path(path).read_text()) for path in args.analysis]
        result = assemble_report(report, forecast, analyses)
        path = Path(args.output).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, result.model_dump_json(indent=2))
        print(
            json.dumps(
                {"status": result.status, "gaps": result.gaps, "result": str(path)},
                ensure_ascii=False,
            )
        )

    guarded(process)


if __name__ == "__main__":
    main()
