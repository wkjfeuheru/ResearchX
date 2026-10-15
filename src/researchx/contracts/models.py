"""Common versioned research types; skill-specific contracts live in their plugins."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Reference(Contract):
    locator: str = Field(min_length=1)
    title: str = Field(min_length=1)
    source_id: str | None = None
    evidence_id: str | None = None
    page: int | None = Field(default=None, ge=1)
    published_at: str | None = None
    excerpt: str = ""


class Company(Contract):
    name: str = Field(min_length=1)
    code: str = Field(pattern=r"^\d{6}$")
    market: Literal["SSE", "SZSE", "BSE"]
    industry: str = Field(min_length=1)
    financial_sector: bool = False
    identity_sources: list[Reference] = Field(min_length=1)

    @model_validator(mode="after")
    def supported(self) -> Company:
        if self.financial_sector:
            raise ValueError("首版仅支持A股非金融企业；金融企业需要专属科目与比率")
        return self


class Result(Contract):
    schema_version: Literal[1, 2, 3] = 1
    company: Company
    inputs: list[Reference] = Field(min_length=1)
    status: Literal["complete", "partial", "blocked"] = "partial"
    gaps: list[str] = Field(default_factory=list)
    as_of: datetime

    @field_validator("as_of")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("as_of requires a timezone")
        return value


class Amount(Contract):
    value: Decimal | None
    unit: Literal["元", "万元", "亿元", "千元", "百万元", "十亿元"] | None = None
    references: list[Reference] = Field(default_factory=list)
    missing_reason: str = ""
    fact_basis: FactBasis | None = None

    @model_validator(mode="after")
    def grounded(self) -> Amount:
        if self.value is None and not self.missing_reason:
            raise ValueError("缺失数值须说明原因")
        if self.value is not None and (
            not self.value.is_finite() or not self.references or self.unit is None
        ):
            raise ValueError("数值须为有限值并附原始资料定位")
        return self


class FactBasis(Contract):
    """Explicit dimensions of a disclosed or calculated financial observation."""

    security_code: str = Field(pattern=r"^\d{6}$")
    market: Literal["SSE", "SZSE", "BSE"]
    period_start: date
    period_end: date
    period_type: Literal["annual", "year_to_date", "single_quarter", "point_in_time"]
    scope: Literal["consolidated", "parent"]
    currency: str = Field(min_length=3, max_length=3)
    unit: Literal["元", "万元", "亿元", "千元", "百万元", "十亿元"] | None = None
    adjustment: Literal["as_reported", "restated"]
    value_kind: Literal["actual", "forecast"]
    as_of: datetime
    published_at: datetime | None = None
    observation_at: datetime | None = None
    benchmark_code: str | None = None
    benchmark_as_of: datetime | None = None
    calculation_version: str | None = None
    formula: str | None = None
    input_evidence_ids: list[str] = Field(default_factory=list)
    metric_id: str = ""

    @model_validator(mode="after")
    def consistent(self) -> FactBasis:
        if self.period_start > self.period_end:
            raise ValueError("财务期间起始日不能晚于结束日")
        if self.as_of.tzinfo is None or (self.published_at and self.published_at.tzinfo is None):
            raise ValueError("资料截止与披露时间必须带时区")
        if self.published_at and self.published_at > self.as_of:
            raise ValueError("不能使用研究截止时间之后披露的数据")
        for timestamp in (self.observation_at, self.benchmark_as_of):
            if timestamp is not None and (timestamp.tzinfo is None or timestamp > self.as_of):
                raise ValueError("行情及基准时间须带时区且不得晚于资料截止时间")
        if self.formula and (not self.calculation_version or not self.input_evidence_ids):
            raise ValueError("派生数值须记录公式版本与输入证据")
        if self.value_kind == "forecast" and not self.calculation_version:
            raise ValueError("预测须记录计算版本")
        return self


def comparable_facts(left: FactBasis, right: FactBasis) -> bool:
    """Never silently compare different security, accounting or period bases."""
    return (
        (
            left.security_code,
            left.market,
            left.scope,
            left.currency,
            left.unit,
            left.period_type,
            left.adjustment,
            left.value_kind,
            left.metric_id,
        )
        == (
            right.security_code,
            right.market,
            right.scope,
            right.currency,
            right.unit,
            right.period_type,
            right.adjustment,
            right.value_kind,
            right.metric_id,
        )
        and left.period_start.month == right.period_start.month
        and left.period_start.day == right.period_start.day
        and left.period_end.month == right.period_end.month
        and left.period_end.day == right.period_end.day
        and left.period_start.year == right.period_start.year + 1
        and left.period_end.year == right.period_end.year + 1
        and left.as_of == right.as_of
    )


Amount.model_rebuild()


class Claim(Contract):
    text: str = Field(min_length=1)
    kind: Literal["fact", "inference", "forecast", "unclassified"] = "unclassified"
    references: list[Reference] = Field(min_length=1)
    observation_ids: list[str] = Field(default_factory=list)
    conclusion_id: str = ""
    reasoning_summary: str = ""
    model_run_id: str = ""
    output_id: str = ""
