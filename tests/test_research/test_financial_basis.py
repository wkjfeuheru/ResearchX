"""Financial values cannot cross security, period, accounting or disclosure boundaries."""

from datetime import date, datetime, timezone
from importlib import import_module

import pytest
from pydantic import ValidationError

from researchx.contracts.models import Amount, FactBasis, comparable_facts

FinancialPeriod = import_module(
    "researchx.skills.bundled.financial-statement-analysis.scripts.models"
).FinancialPeriod


def basis(**changes: object) -> FactBasis:
    values = {
        "security_code": "300750", "market": "SZSE",
        "period_start": date(2025, 1, 1), "period_end": date(2025, 12, 31),
        "period_type": "annual", "scope": "consolidated", "currency": "CNY",
        "adjustment": "as_reported", "value_kind": "actual", "unit": "亿元",
        "as_of": datetime(2026, 4, 1, tzinfo=timezone.utc),
        "published_at": datetime(2026, 3, 1, tzinfo=timezone.utc),
    }
    return FactBasis.model_validate({**values, **changes})


def test_financial_basis_rejects_future_and_unversioned_derived_numbers():
    with pytest.raises(ValidationError, match="截止时间之后"):
        basis(published_at=datetime(2026, 5, 1, tzinfo=timezone.utc))
    with pytest.raises(ValidationError, match="公式版本"):
        basis(formula="revenue-cost")


def test_cross_period_comparison_requires_matching_basis():
    current = basis()
    prior = basis(period_start=date(2024, 1, 1), period_end=date(2024, 12, 31))
    assert comparable_facts(current, prior)
    assert not comparable_facts(current, prior.model_copy(update={"currency": "USD"}))
    assert not comparable_facts(current, prior.model_copy(update={"period_type": "year_to_date"}))
    assert not comparable_facts(current, prior.model_copy(update={"adjustment": "restated"}))
    assert not comparable_facts(current, prior.model_copy(update={"scope": "parent"}))
    assert not comparable_facts(current, prior.model_copy(update={"unit": "万元"}))


def test_period_rejects_mixed_currency_and_scope():
    amount = Amount.model_validate({
        "value": 100, "unit": "元",
        "references": [{"locator": "https://www.szse.cn/report", "title": "年报", "evidence_id": "ev_1"}],
        "fact_basis": basis(currency="USD").model_dump(mode="json"),
    })
    with pytest.raises(ValidationError, match="口径与财务期间不一致"):
        FinancialPeriod(label="FY2025", start=date(2025, 1, 1), end=date(2025, 12, 31),
                        currency="CNY", scope="consolidated", items={"revenue": amount})
