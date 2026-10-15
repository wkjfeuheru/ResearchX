"""Versioned contracts owned by financial-statement-analysis."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal
from pydantic import Field, field_validator, model_validator
from researchx.contracts import Amount, Contract, Reference, Result
from researchx.contracts import restore_decimal_fields


class FinancialPeriod(Contract):
    label: str
    start: date
    end: date
    currency: str | None = None
    scope: Literal["consolidated", "parent"] = "consolidated"
    restated: bool = False
    # Exact aliases are in references/accounting.md; unknown keys fail validation.
    items: dict[str, Amount]
    rounding_unit: Decimal = Field(default=Decimal("1"), gt=0)

    @model_validator(mode="after")
    def valid_period(self) -> FinancialPeriod:
        if self.end < self.start:
            raise ValueError("财务期间结束日不能早于开始日")
        from .analyze_statements import CORE_ITEMS

        if unknown := set(self.items) - set(CORE_ITEMS):
            raise ValueError(f"未知财务科目: {sorted(unknown)}")
        for key, amount in self.items.items():
            basis = amount.fact_basis
            if basis and (
                basis.period_start != self.start
                or basis.period_end != self.end
                or basis.scope != self.scope
                or basis.currency != self.currency
                or (basis.unit is not None and basis.unit != amount.unit)
                or (basis.adjustment == "restated") != self.restated
            ):
                raise ValueError(f"{key}: 金额口径与财务期间不一致")
        return self


class Finding(Contract):
    topic: str
    fact: str
    interpretation: str
    references: list[Reference] = Field(min_length=1)
    gaps: list[str] = Field(default_factory=list)


class FinancialResult(Result):
    kind: Literal["financial"] = "financial"
    periods: list[FinancialPeriod] = Field(min_length=1)
    notes: list[Finding] = Field(default_factory=list)
    ratios: list[dict[str, object]] = Field(default_factory=list)
    checks: list[dict[str, object]] = Field(default_factory=list)
    changes: list[dict[str, object]] = Field(default_factory=list)

    @model_validator(mode="after")
    def explicit_v2_basis(self) -> FinancialResult:
        if self.schema_version == 3:
            for period in self.periods:
                if not period.currency:
                    continue
                for key, amount in period.items.items():
                    if amount.value is None:
                        continue
                    basis = amount.fact_basis
                    if (
                        basis is None
                        or basis.security_code != self.company.code
                        or basis.market != self.company.market
                        or basis.unit is None
                    ):
                        raise ValueError(f"{period.label}/{key}: 必须提供同一证券的财务口径")
                    if basis.value_kind != "actual" or basis.as_of != self.as_of:
                        raise ValueError(f"{period.label}/{key}: 财报值须为截止时已披露的实际值")
                    if any(ref.evidence_id is None for ref in amount.references):
                        raise ValueError(f"{period.label}/{key}: 财报值须绑定输入证据 ID")
        return self

    @field_validator("ratios", "checks", "changes")
    @classmethod
    def computed_numbers(cls, value: list[dict[str, object]]) -> list[dict[str, object]]:
        return restore_decimal_fields(
            value,
            (
                "*.value",
                "*.inputs_yuan.*",
                "*.residual_yuan",
                "*.tolerance_yuan",
                "*.difference_yuan",
                "*.yoy",
            ),
        )
