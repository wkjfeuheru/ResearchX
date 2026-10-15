"""Versioned contracts owned by earnings-forecast (legacy DeepResult contract)."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal
from pydantic import Field, field_validator, model_validator
from researchx.contracts import Amount, Claim, Contract, Reference, Result
from researchx.contracts import restore_decimal_fields


class Assumption(Contract):
    value: Decimal | None
    rationale: str = Field(min_length=1)
    origin: Literal["historical", "external", "analyst"]
    references: list[Reference] = Field(default_factory=list)

    @model_validator(mode="after")
    def grounded(self) -> Assumption:
        if self.value is not None and not self.value.is_finite():
            raise ValueError("假设须为有限值")
        if self.origin != "analyst" and not self.references:
            raise ValueError("历史或外部假设须附依据")
        return self


class ForecastYear(Contract):
    year: int
    assumptions: dict[str, Assumption]


class Scenario(Contract):
    name: Literal["base", "optimistic", "cautious"]
    years: list[ForecastYear] = Field(min_length=3, max_length=3)


class ReportSection(Contract):
    key: Literal[
        "summary",
        "business",
        "industry",
        "financial_quality",
        "events",
        "forecast",
        "risks",
        "sources",
    ]
    title: str
    paragraphs: list[Claim] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class DeepResult(Result):
    kind: Literal["deep"] = "deep"
    base_year: int
    base_revenue: Amount
    base_currency: str | None = None
    base_scope: Literal["consolidated"] = "consolidated"
    scenarios: list[Scenario] = Field(default_factory=list)
    sections: list[ReportSection] = Field(default_factory=list)
    prior_results: list[Reference] = Field(default_factory=list)
    forecasts: list[dict[str, object]] = Field(default_factory=list)
    sensitivity: list[dict[str, object]] = Field(default_factory=list)

    @model_validator(mode="after")
    def explicit_forecast_basis(self) -> DeepResult:
        if self.schema_version != 3 or self.base_revenue.value is None:
            return self
        basis = self.base_revenue.fact_basis
        if basis is None or (
            basis.security_code != self.company.code
            or basis.market != self.company.market
            or basis.scope != self.base_scope
            or basis.currency != self.base_currency
            or basis.unit != self.base_revenue.unit
            or basis.value_kind != "actual"
            or basis.period_type != "annual"
            or basis.period_end.year != self.base_year
            or basis.as_of != self.as_of
        ):
            raise ValueError("预测基期须绑定同一证券、年度、单位、币种与报表范围的实际值")
        if any(ref.evidence_id is None for ref in self.base_revenue.references):
            raise ValueError("预测基期必须绑定输入证据")
        for scenario in self.scenarios:
            for year in scenario.years:
                for assumption in year.assumptions.values():
                    if (
                        assumption.origin != "analyst"
                        and assumption.value is not None
                        and any(ref.evidence_id is None for ref in assumption.references)
                    ):
                        raise ValueError("历史或外部预测假设必须绑定输入证据")
        return self

    @field_validator("forecasts", "sensitivity")
    @classmethod
    def computed_numbers(cls, value: list[dict[str, object]]) -> list[dict[str, object]]:
        return restore_decimal_fields(
            value,
            (
                "*.previous_revenue",
                "*.values.*",
                "*.inputs.*",
                "*.assumptions.*.value",
                "*.growth_shift",
                "*.gross_margin_shift",
                "*.parent_net_profit",
            ),
        )


# Retain the established model name/schema and kind for existing consumers.
ForecastResult = DeepResult
