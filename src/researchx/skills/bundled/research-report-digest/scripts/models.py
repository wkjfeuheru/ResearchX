"""Versioned contracts owned by research-report-digest."""

from __future__ import annotations

from datetime import date
from typing import Literal
from pydantic import Field, field_validator, model_validator
from researchx.contracts import Amount, Claim, Contract, Reference, Result
from researchx.contracts import restore_decimal_fields


class Prediction(Contract):
    year: int = Field(ge=2000, le=2100)
    metric: str
    amount: Amount
    currency: str | None = None
    basis: str = Field(min_length=1)


class Rating(Contract):
    current: str | None = None
    previous: str | None = None
    change_explicit: bool = False
    references: list[Reference] = Field(default_factory=list)

    @model_validator(mode="after")
    def grounded(self) -> Rating:
        if (self.current or self.previous) and not self.references:
            raise ValueError("评级须有原文依据")
        return self


class BrokerReport(Contract):
    title: str
    institution: str | None
    authors: list[str] = Field(default_factory=list)
    published_at: date | None
    references: list[Reference] = Field(min_length=1)
    summary_only: bool = False
    views: list[Claim] = Field(default_factory=list)
    arguments: list[Claim] = Field(default_factory=list)
    assumptions: list[Claim] = Field(default_factory=list)
    predictions: list[Prediction] = Field(default_factory=list)
    target_price: Amount | None = None
    target_currency: str | None = None
    rating: Rating = Field(default_factory=Rating)
    rating_change: str | None = None
    risks: list[Claim] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class DigestResult(Result):
    kind: Literal["digest"] = "digest"
    reports: list[BrokerReport] = Field(min_length=1)
    comparison: list[dict[str, object]] = Field(default_factory=list)

    @field_validator("comparison")
    @classmethod
    def computed_numbers(cls, value: list[dict[str, object]]) -> list[dict[str, object]]:
        return restore_decimal_fields(value, ("*.predictions.*.value_yuan",))
