"""Deterministic financial-statement-analysis processing."""

from __future__ import annotations

# Direct file execution resolves the same package as an installed module.
if not __package__:
    from researchx.skills.runtime_support import script_package

    __package__ = script_package(__file__)

from decimal import Decimal
from researchx.contracts import amount_value
from researchx.contracts.models import comparable_facts
from .models import FinancialResult, FinancialPeriod
from researchx.skills.runtime_support import processing_main

CORE_ITEMS = {
    "cash": "货币资金",
    "receivables": "应收账款",
    "inventory": "存货",
    "current_assets": "流动资产",
    "total_assets": "资产总计",
    "current_liabilities": "流动负债",
    "total_liabilities": "负债合计",
    "total_equity": "股东权益合计",
    "parent_equity": "归母权益",
    "opening_parent_equity": "期初归母权益",
    "revenue": "营业收入",
    "cost": "营业成本",
    "tax_surcharges": "税金及附加",
    "selling_expense": "销售费用",
    "admin_expense": "管理费用",
    "rd_expense": "研发费用",
    "finance_expense": "财务费用",
    "other_operating_net": "其他经营损益净额",
    "operating_profit": "营业利润",
    "non_operating_net": "营业外收支净额",
    "pretax_profit": "利润总额",
    "income_tax": "所得税",
    "net_profit": "合并净利润",
    "parent_net_profit": "归母净利润",
    "minority_profit": "少数股东损益",
    "adjusted_parent_profit": "扣非归母净利润",
    "operating_cashflow": "经营现金流净额",
    "investing_cashflow": "投资现金流净额",
    "financing_cashflow": "筹资现金流净额",
    "fx_cash_effect": "汇率对现金的影响",
    "net_cash_change": "现金净变动",
}
NOTE_TOPICS = {"非经常损益", "应收账款", "存货", "减值", "关联交易"}


def calculate_financial(result: FinancialResult) -> FinancialResult:
    result.ratios, result.checks, result.changes = [], [], []
    for period in result.periods:
        if not period.currency:
            result.gaps.append(f"{period.label}/{period.scope}: 币种未知，不推定为人民币")

        def value(key: str) -> Decimal | None:
            return amount_value(period.items.get(key))

        missing = set(CORE_ITEMS) - set(period.items)
        if missing:
            result.gaps.append(
                f"{period.label}/{period.scope}: 未取得科目 "
                + "/".join(CORE_ITEMS[key] for key in sorted(missing))
            )
        result.gaps.extend(
            f"{period.label}/{period.scope}/{CORE_ITEMS[key]}: {item.missing_reason}"
            for key, item in period.items.items()
            if item.value is None
        )
        for name, numerator_keys, denominator_keys, formula in (
            ("gross_margin", ["revenue", "cost"], ["revenue"], "(revenue-cost)/revenue"),
            ("net_margin", ["net_profit"], ["revenue"], "net_profit/revenue"),
            (
                "debt_ratio",
                ["total_liabilities"],
                ["total_assets"],
                "total_liabilities/total_assets",
            ),
            (
                "current_ratio",
                ["current_assets"],
                ["current_liabilities"],
                "current_assets/current_liabilities",
            ),
            (
                "cash_to_profit",
                ["operating_cashflow"],
                ["net_profit"],
                "operating_cashflow/net_profit",
            ),
            (
                "simple_roe",
                ["parent_net_profit"],
                ["opening_parent_equity", "parent_equity"],
                "parent_net_profit/((opening_parent_equity+parent_equity)/2)",
            ),
        ):
            keys = list(dict.fromkeys(numerator_keys + denominator_keys))
            inputs = {key: value(key) for key in keys}
            reason, ratio = "", None
            if any(item is None for item in inputs.values()):
                reason = "缺少同口径输入"
            else:
                checked = _complete_amounts(inputs)
                denominator = sum((checked[key] for key in denominator_keys), Decimal(0)) / len(
                    denominator_keys
                )
                numerator = checked[numerator_keys[0]]
                if name == "gross_margin":
                    numerator -= checked["cost"]
                if denominator == 0:
                    reason = "分母为零"
                elif name == "simple_roe" and any(checked[key] <= 0 for key in denominator_keys):
                    reason = "期初或期末归母权益非正，ROE不可解释"
                else:
                    ratio = numerator / denominator
                    if denominator < 0:
                        reason = "分母为负，解释受限"
            result.ratios.append(
                {
                    "period": period.label,
                    "scope": period.scope,
                    "currency": period.currency,
                    "metric": name,
                    "value": ratio,
                    "formula": formula,
                    "inputs_yuan": inputs,
                    "references": [
                        reference.model_dump()
                        for key in keys
                        if key in period.items
                        for reference in period.items[key].references
                    ],
                    "annualized": False,
                    "limitation": reason,
                }
            )
        checks = (
            ("balance_sheet", "total_assets", ["total_liabilities", "total_equity"]),
            ("profit_total", "pretax_profit", ["operating_profit", "non_operating_net"]),
            ("net_profit", "pretax_profit", ["net_profit", "income_tax"]),
            ("attribution", "net_profit", ["parent_net_profit", "minority_profit"]),
            (
                "cash_flow",
                "net_cash_change",
                [
                    "operating_cashflow",
                    "investing_cashflow",
                    "financing_cashflow",
                    "fx_cash_effect",
                ],
            ),
        )
        for name, target, parts in checks:
            keys = [target, *parts]
            values = [value(key) for key in keys]
            # Each disclosed number can be rounded by half of the declared yuan precision.
            tolerance = period.rounding_unit * Decimal(len(keys)) / 2
            residual = None
            if all(v is not None for v in values):
                checked_values = [v for v in values if v is not None]
                residual = checked_values[0] - sum(checked_values[1:])
            state = (
                "missing" if residual is None else "pass" if abs(residual) <= tolerance else "fail"
            )
            result.checks.append(
                {
                    "period": period.label,
                    "scope": period.scope,
                    "check": name,
                    "state": state,
                    "residual_yuan": residual,
                    "tolerance_yuan": tolerance,
                    "inputs_yuan": dict(zip(keys, values)),
                    "references": [
                        ref.model_dump()
                        for key in keys
                        if key in period.items
                        for ref in period.items[key].references
                    ],
                    "formula": target + " = " + " + ".join(parts),
                }
            )
            if state == "fail":
                result.gaps.append(
                    f"{period.label}/{period.scope}: {name}勾稽不通过，保留原值待复核"
                )
        # Signed reconciliation of the core operating-profit bridge.
        terms = {
            "revenue": 1,
            "cost": -1,
            "tax_surcharges": -1,
            "selling_expense": -1,
            "admin_expense": -1,
            "rd_expense": -1,
            "finance_expense": -1,
            "other_operating_net": 1,
            "operating_profit": -1,
        }
        inputs = {key: value(key) for key in terms}
        residual = (
            None
            if any(v is None for v in inputs.values())
            else sum(
                (_complete_amounts(inputs)[key] * sign for key, sign in terms.items()), Decimal(0)
            )
        )
        tolerance = period.rounding_unit * Decimal(len(terms)) / 2
        state = "missing" if residual is None else "pass" if abs(residual) <= tolerance else "fail"
        result.checks.append(
            {
                "period": period.label,
                "scope": period.scope,
                "check": "operating_profit",
                "state": state,
                "residual_yuan": residual,
                "tolerance_yuan": tolerance,
                "inputs_yuan": inputs,
                "formula": "revenue-cost-tax_surcharges-selling_expense-admin_expense-rd_expense-finance_expense+other_operating_net=operating_profit",
                "references": [
                    ref.model_dump()
                    for key in terms
                    if key in period.items
                    for ref in period.items[key].references
                ],
            }
        )
        if state == "fail":
            result.gaps.append(
                f"{period.label}/{period.scope}: operating_profit勾稽不通过，保留原值待复核"
            )

    # Match same period length one year earlier; never compare parent to consolidated.
    def superseded(period: FinancialPeriod) -> bool:
        return not period.restated and any(
            other.restated
            and other.start == period.start
            and other.end == period.end
            and other.scope == period.scope
            and other.currency == period.currency
            for other in result.periods
        )

    for current in result.periods:
        for prior in result.periods:
            if (
                superseded(current)
                or superseded(prior)
                or not current.currency
                or current.scope != prior.scope
                or current.currency != prior.currency
                or current.start.year != prior.start.year + 1
                or current.end.year != prior.end.year + 1
                or (current.start.month, current.start.day, current.end.month, current.end.day)
                != (prior.start.month, prior.start.day, prior.end.month, prior.end.day)
            ):
                continue
            for key in ("revenue", "net_profit", "parent_net_profit"):
                left = current.items.get(key)
                right = prior.items.get(key)
                left_metric = (left.fact_basis.metric_id if left and left.fact_basis and left.fact_basis.metric_id else key)
                right_metric = (right.fact_basis.metric_id if right and right.fact_basis and right.fact_basis.metric_id else key)
                if (
                    left is None
                    or right is None
                    or left.fact_basis is None
                    or right.fact_basis is None
                    or left_metric != right_metric
                    or not comparable_facts(left.fact_basis, right.fact_basis)
                ):
                    result.gaps.append(f"{current.label}/{key}: 跨期口径不一致，不计算同比")
                    continue
                now, before = (
                    amount_value(current.items.get(key)),
                    amount_value(prior.items.get(key)),
                )
                if now is None or before is None:
                    continue
                change = "亏损扩大" if now < before else "亏损收窄" if now > before else "亏损不变"
                if not (now < 0 and before < 0):
                    change = (
                        "扭亏"
                        if before < 0 <= now
                        else "转亏"
                        if now < 0 <= before
                        else "增加"
                        if now > before
                        else "减少"
                        if now < before
                        else "不变"
                    )
                result.changes.append(
                    {
                        "period": current.label,
                        "prior": prior.label,
                        "scope": current.scope,
                        "metric": key,
                        "currency": current.currency,
                        "prior_restated": prior.restated,
                        "inputs_yuan": {"current": now, "prior": before},
                        "formula": "difference=current-prior; yoy=(current-prior)/prior (positive prior only)",
                        "calculation_version": "financial-comparison-v3",
                        "input_evidence_ids": [
                            ref.evidence_id
                            for period in (current, prior)
                            for ref in period.items[key].references
                            if ref.evidence_id
                        ],
                        "references": [
                            ref.model_dump()
                            for period in (current, prior)
                            for ref in period.items[key].references
                        ],
                        "difference_yuan": now - before,
                        "yoy": (now - before) / before if before > 0 else None,
                        "direction": change,
                        "limitation": "基期非正，不报告常规同比" if before <= 0 else "",
                    }
                )
    for topic in sorted(NOTE_TOPICS - {note.topic for note in result.notes}):
        result.gaps.append(f"{topic}: 关键附注尚未取得或核查")
    result.gaps.extend(gap for note in result.notes for gap in note.gaps)
    result.gaps = list(dict.fromkeys(result.gaps))
    result.status = (
        "partial" if result.gaps or any(r["value"] is None for r in result.ratios) else "complete"
    )
    return result


def main(argv: list[str] | None = None) -> None:
    processing_main(FinancialResult, calculate_financial, argv)


def _complete_amounts(values: dict[str, Decimal | None]) -> dict[str, Decimal]:
    if any(value is None for value in values.values()):
        raise ValueError("Calculation requires complete amounts")
    return {key: value for key, value in values.items() if value is not None}


if __name__ == "__main__":
    main()
