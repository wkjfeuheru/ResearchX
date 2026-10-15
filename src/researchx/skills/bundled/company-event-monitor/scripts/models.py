"""Versioned contracts owned by company-event-monitor."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from pydantic import Field, field_validator
from researchx.contracts import Contract, Reference, Result


class Score(Contract):
    value: int | None = Field(ge=-2, le=2, strict=True)
    reason: str = Field(min_length=1)
    confidence: Literal["high", "medium", "low"]


class Event(Contract):
    event_key: str = Field(
        min_length=1, description="Stable company/category/event identity, not article URL"
    )
    title: str
    occurred_at: datetime | None = None
    published_at: datetime | None = None
    category: Literal[
        "merger",
        "shareholding",
        "earnings_guidance",
        "financial_report",
        "financing_dividend",
        "litigation_regulation",
        "operating_contract",
        "management",
        "other",
    ]
    summary: str
    references: list[Reference] = Field(min_length=1)
    sentiment: Score
    impact: Score
    summary_only: bool = False

    @field_validator("occurred_at", "published_at")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value and value.tzinfo is None:
            raise ValueError("事件日期须带时区")
        return value


class Channel(Contract):
    name: str
    state: Literal["ok", "failed", "stale", "summary_only"]
    detail: str = ""


class MonitorResult(Result):
    kind: Literal["monitor"] = "monitor"
    window_start: datetime | None = None
    events: list[Event] = Field(default_factory=list)
    undated_events: list[Event] = Field(default_factory=list)
    outside_window: list[Event] = Field(default_factory=list)
    channels: list[Channel] = Field(min_length=1)
