"""Deterministic earnings forecasting; legacy DeepResult JSON remains accepted."""

from __future__ import annotations

# Direct file execution resolves the same package as an installed module.
if not __package__:
    from researchx.skills.runtime_support import script_package

    __package__ = script_package(__file__)


from decimal import Decimal
from researchx.contracts import amount_value
from .models import DeepResult
from researchx.skills.runtime_support import processing_main
from typing import cast
from typing_extensions import TypedDict


REQUIRED_ASSUMPTIONS = {
    "growth",
    "gross_margin",
    "selling_rate",
    "admin_rate",
    "rd_rate",
    "finance_rate",
    "tax_surcharge_rate",
    "other_operating_net",
    "non_operating_net",
    "tax_rate",
    "parent_share",
}
SECTION_KEYS = {
    "summary",
    "business",
    "industry",
    "financial_quality",
    "events",
    "forecast",
    "risks",
    "sources",
}


class ProjectionInputs(TypedDict):
    growth: Decimal
    gross_margin: Decimal
    tax_rate: Decimal
    parent_share: Decimal
    selling_rate: Decimal
    admin_rate: Decimal
    rd_rate: Decimal
    finance_rate: Decimal
    tax_surcharge_rate: Decimal
    other_operating_net: Decimal
    non_operating_net: Decimal
    shares: Decimal | None


def project_year(revenue: Decimal, values: ProjectionInputs) -> dict[str, Decimal | None]:
    revenue = revenue * (1 + values["growth"])
    gross_profit = revenue * values["gross_margin"]
    expenses = revenue * sum(
        values[name]
        for name in ("selling_rate", "admin_rate", "rd_rate", "finance_rate", "tax_surcharge_rate")
    )
    operating_profit = gross_profit - expenses + values["other_operating_net"]
    pretax = operating_profit + values["non_operating_net"]
    net_profit = pretax * (1 - values["tax_rate"])
    parent_profit = net_profit * values["parent_share"]
    shares = values.get("shares")
    return {
        "revenue": revenue,
        "gross_profit": gross_profit,
        "operating_profit": operating_profit,
        "pretax_profit": pretax,
        "net_profit": net_profit,
        "parent_net_profit": parent_profit,
        "eps": parent_profit / shares if shares is not None and shares > 0 else None,
    }


def calculate_forecast(result: DeepResult) -> DeepResult:
    result.forecasts, result.sensitivity = [], []
    invalid_base = result.base_year != result.as_of.year - 1 or not result.base_currency
    if invalid_base:
        result.gaps.append(
            "预测基期须为截止时间最近已结束的完整财年；旧基期或当期累计收入不足以形成未来三年预测"
        )
    base_revenue = amount_value(result.base_revenue)
    names = [scenario.name for scenario in result.scenarios]
    if set(names) != {"base", "optimistic", "cautious"} or len(names) != 3:
        result.gaps.append("基准、乐观、谨慎三种情景未全部提供")
    for scenario in result.scenarios:
        expected_years = list(range(result.base_year + 1, result.base_year + 4))
        if [year.year for year in scenario.years] != expected_years:
            raise ValueError("预测必须覆盖基期后的连续三个完整财年")
        revenue = base_revenue
        for year in scenario.years:
            unknown = set(year.assumptions) - REQUIRED_ASSUMPTIONS - {"shares"}
            if unknown:
                raise ValueError(f"未知预测假设: {sorted(unknown)}")
            missing = REQUIRED_ASSUMPTIONS - set(year.assumptions)
            values = {key: item.value for key, item in year.assumptions.items()}
            missing |= {key for key in REQUIRED_ASSUMPTIONS if values.get(key) is None}
            if missing or revenue is None or invalid_base:
                result.gaps.append(
                    f"{scenario.name}/{year.year}: 预测基期或假设缺失 {sorted(missing)}"
                )
                revenue = None
                continue
            inputs = cast(ProjectionInputs, values)
            if "shares" in values and (
                inputs["shares"] is None
                or inputs["shares"] <= 0
                or year.assumptions["shares"].origin == "analyst"
                or not year.assumptions["shares"].references
            ):
                inputs["shares"] = None
                result.gaps.append(f"{scenario.name}/{year.year}: 股本缺少可靠依据，EPS不计算")
            for key in ("gross_margin", "tax_rate", "parent_share"):
                if not 0 <= inputs[key] <= 1:
                    raise ValueError(f"{key}必须在0到1之间")
            if inputs["growth"] < -1:
                raise ValueError("收入增速不可低于-100%")
            for key, number in (
                ("selling_rate", inputs["selling_rate"]),
                ("admin_rate", inputs["admin_rate"]),
                ("rd_rate", inputs["rd_rate"]),
                ("tax_surcharge_rate", inputs["tax_surcharge_rate"]),
            ):
                if number < 0:
                    raise ValueError(f"{key}不可为负")
            projection = project_year(revenue, inputs)
            result.forecasts.append(
                {
                    "scenario": scenario.name,
                    "year": year.year,
                    "currency": result.base_currency,
                    "unit": "元",
                    "previous_revenue": revenue,
                    "base_references": [ref.model_dump() for ref in result.base_revenue.references],
                    "values": projection,
                    "calculation_version": "earnings-forecast-v2",
                    "input_evidence_ids": sorted(
                        {
                            ref.evidence_id
                            for ref in result.base_revenue.references
                            if ref.evidence_id
                        }
                        | {
                            ref.evidence_id
                            for assumption in year.assumptions.values()
                            for ref in assumption.references
                            if ref.evidence_id
                        }
                    ),
                    "inputs": values,
                    "assumptions": {
                        key: item.model_dump() for key, item in year.assumptions.items()
                    },
                    "formulas": {
                        "revenue": "previous_revenue*(1+growth)",
                        "operating_profit": "revenue*(gross_margin-selling_rate-admin_rate-rd_rate-finance_rate-tax_surcharge_rate)+other_operating_net",
                        "pretax_profit": "operating_profit+non_operating_net",
                        "net_profit": "pretax_profit*(1-tax_rate)",
                        "parent_net_profit": "net_profit*parent_share",
                        "eps": "parent_net_profit/shares (reliable shares only)",
                    },
                    "limitation": "税率为简化有效税率假设，不推演递延所得税或完整三大表",
                }
            )
            if scenario.name == "base":
                for growth_shift in (Decimal("-0.02"), Decimal(0), Decimal("0.02")):
                    for margin_shift in (Decimal("-0.02"), Decimal(0), Decimal("0.02")):
                        changed: ProjectionInputs = {
                            **inputs,
                            "growth": max(Decimal(-1), inputs["growth"] + growth_shift),
                            "gross_margin": min(
                                Decimal(1), max(Decimal(0), inputs["gross_margin"] + margin_shift)
                            ),
                        }
                        sensitivity = project_year(revenue, changed)
                        result.sensitivity.append(
                            {
                                "year": year.year,
                                "growth_shift": growth_shift,
                                "gross_margin_shift": margin_shift,
                                "parent_net_profit": sensitivity["parent_net_profit"],
                                "basis": "单年度冲击，其他输入及上一年收入保持基准情景",
                                "previous_revenue": revenue,
                                "inputs": changed,
                                "base_references": [
                                    ref.model_dump() for ref in result.base_revenue.references
                                ],
                            }
                        )
            revenue = projection["revenue"]
    result.gaps = list(dict.fromkeys(result.gaps))
    result.status = "partial" if result.gaps else "complete"
    return result


def main(argv: list[str] | None = None) -> None:
    processing_main(DeepResult, calculate_forecast, argv)


if __name__ == "__main__":
    main()
