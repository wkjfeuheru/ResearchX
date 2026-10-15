"""Deterministic research-report-digest processing."""

from __future__ import annotations

# Direct file execution resolves the same package as an installed module.
if not __package__:
    from researchx.skills.runtime_support import script_package

    __package__ = script_package(__file__)

from collections import defaultdict
from researchx.contracts import amount_value
from .models import DigestResult
from researchx.skills.runtime_support import processing_main


def normalize_digest(result: DigestResult) -> DigestResult:
    groups = defaultdict(list)
    for report in result.reports:
        if not report.institution or not report.published_at:
            report.gaps.append("机构或发布日期未知")
        if report.summary_only:
            report.gaps.append("仅取得摘要，未核验完整报告")
        if not report.views or not report.arguments or not report.risks:
            report.gaps.append("观点、论据或风险提示字段不完整")
        if not report.predictions:
            report.gaps.append("未取得盈利预测")
        if not report.authors:
            report.gaps.append("作者未知")
        if report.target_price is None:
            report.gaps.append("原文未提供或未取得目标价")
        elif not report.target_currency:
            report.gaps.append("目标价币种未知")
        if report.rating.current is None:
            report.gaps.append("原文未提供或未取得评级")
        if report.rating_change and not report.rating.change_explicit:
            # Comparing prior reports requires same institution and earlier date.
            matched = any(
                report.institution
                and other is not report
                and other.institution == report.institution
                and other.published_at
                and report.published_at
                and other.published_at < report.published_at
                and other.rating.current == report.rating.previous
                and report.rating.previous is not None
                for other in result.reports
            )
            if not matched:
                report.rating_change = None
                report.gaps.append("缺少同机构前次评级依据，评级变动未知")
        for prediction in report.predictions:
            if not prediction.currency or prediction.amount.value is None:
                report.gaps.append(
                    f"{prediction.year}/{prediction.metric}: 数值或币种未知，不作跨篇比较"
                )
                continue
            value = amount_value(prediction.amount)
            groups[
                (prediction.year, prediction.metric, prediction.currency, prediction.basis)
            ].append(
                {
                    "report": report.title,
                    "institution": report.institution,
                    "published_at": str(report.published_at) if report.published_at else None,
                    "value_yuan": value,
                    "references": [ref.model_dump() for ref in prediction.amount.references],
                }
            )
        result.gaps.extend(f"{report.title}: {gap}" for gap in report.gaps)
    result.comparison = [
        {
            "year": key[0],
            "metric": key[1],
            "currency": key[2],
            "basis": key[3],
            "predictions": values,
        }
        for key, values in groups.items()
    ]
    result.gaps = list(dict.fromkeys(result.gaps))
    result.status = "partial" if result.gaps else "complete"
    return result


def main(argv: list[str] | None = None) -> None:
    processing_main(DigestResult, normalize_digest, argv)


if __name__ == "__main__":
    main()
