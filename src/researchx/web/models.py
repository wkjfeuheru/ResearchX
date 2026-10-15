"""Validated inputs for the local web API."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class ModelInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    label: str = Field(min_length=1, max_length=100)
    api_format: Literal["openai", "anthropic"]
    model: str = Field(min_length=1, max_length=200)
    base_url: str | None = Field(default=None, max_length=2000)
    api_key: SecretStr | None = None
    context_window_tokens: int | None = Field(default=None, gt=0)
    auto_compact_threshold_tokens: int | None = Field(default=None, gt=0)

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value: str | None) -> str | None:
        if not value:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("请输入完整的 HTTP 或 HTTPS 地址")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("接口地址不能包含凭据、查询参数或片段")
        return value.rstrip("/")


class SkillToggle(BaseModel):
    enabled: bool


class SessionInput(BaseModel):
    profile_id: str | None = Field(default=None, max_length=100)


class CommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["submit", "cancel", "response", "steer", "resume"]
    request_id: str = Field(min_length=1, max_length=100)
    target_request_id: str | None = Field(default=None, max_length=100)
    text: str = Field(default="", max_length=100_000)
    profile_id: str | None = Field(default=None, max_length=100)
    research_type: Literal[
        "event_commentary",
        "financial_commentary",
        "financial-statement-analysis",
        "financial-commentary",
        "company-event-monitor",
        "research-report-digest",
        "earnings-forecast",
        "industry-commentary",
        "industry-deep-dive",
        "deep-investment-report",
        "generic-research",
    ] | None = None
    attachment_ids: list[str] = Field(default_factory=list, max_length=10)
    prompt_id: str | None = Field(default=None, max_length=100)
    answer: str = Field(default="", max_length=100_000)
    run_id: str | None = Field(default=None, max_length=100)
    epoch: int | None = Field(default=None, ge=0)


class ReviewRequest(BaseModel):
    """Browser review payload. Actor identity is attached by the local adapter."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "changes_requested"]
    expected_manifest_hash: str = Field(min_length=64, max_length=64)
    note: str = Field(min_length=1, max_length=4000)
    reviewed_claim_ids: list[str] = Field(default_factory=list, max_length=200)
