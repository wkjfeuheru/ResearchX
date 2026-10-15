"""Versioned evaluation cases, observations and durable run results."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Category = Literal["financial", "events", "digest", "deep", "cross"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceAsset(Record):
    id: str
    path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    locator: str
    title: str
    family: str
    published_at: str | None = None
    page: int | None = None
    provenance: Literal["synthetic", "official_snapshot", "live"]
    available_from_turn: int = Field(default=0, ge=0)
    replaces_asset_id: str | None = None


class Requirement(Record):
    id: str
    description: str
    check: Literal["semantic", "numeric", "artifact", "limitation"] = "semantic"
    required: bool = True
    value: str | None = None
    unit: str = ""
    currency: str = ""
    period: str = ""
    scope: str = ""
    atol: str = "0.01"
    rtol: str = "0.0001"
    source_ids: list[str] = Field(default_factory=list)
    source_locators: list[str] = Field(default_factory=list)


class PathRules(Record):
    tool_groups: list[list[str]] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    scripts: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    forbidden_behaviors: list[str] = Field(default_factory=list)
    plan_before_collection: bool = False
    verification_required: bool = True
    order: list[tuple[str, str]] = Field(default_factory=list)
    conflict: Literal["none", "detect", "resolve", "unresolved", "reopen"] = "none"
    conflict_kind: Literal["scope", "fact", "calculation", "interpretation"] | None = None
    equivalence_notes: str = "允许能够满足同一需求的等价工具组合。"


class Turn(Record):
    prompt: str = Field(min_length=1)
    action: Literal["submit", "steer", "cancel_resume", "restart"] = "submit"
    trigger: Literal["after_turn", "first_tool", "first_collection"] = "after_turn"
    question_response: str = "按给定历史期间、币种、口径和资料完成；缺失项保留未知与原因。"


class Budget(Record):
    timeout_seconds: int = Field(default=900, ge=1)
    model_calls: int = Field(default=64, ge=1)
    tool_calls: int = Field(default=160, ge=1)
    total_tokens: int = Field(default=1000000, ge=1)


class Fault(Record):
    tool: str
    occurrence: int = Field(default=1, ge=1)
    kind: Literal["tool_error", "timeout", "investigation_timeout"]
    recovery: str


class EvalCase(Record):
    id: str
    version: str = "1.0.0"
    category: Category
    family: str
    split: Literal["dev", "holdout"]
    difficulty: Literal["basic", "intermediate", "complex"]
    environment: Literal["fixed", "live"]
    material: Literal["synthetic", "snapshot", "live"]
    title: str
    turns: list[Turn] = Field(min_length=1)
    cutoff: str
    assets: list[SourceAsset] = Field(default_factory=list)
    live_urls: list[str] = Field(default_factory=list)
    disabled_skills: list[str] = Field(default_factory=list)
    requirements: list[Requirement] = Field(min_length=1)
    reference_facts: list[str] = Field(min_length=1)
    reference_locations: dict[str, list[str]] = Field(default_factory=dict)
    path: PathRules
    budget: Budget = Field(default_factory=Budget)
    faults: list[Fault] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    annotation_basis: str = Field(min_length=1)
    review_status: Literal["rule_checked", "source_checked", "human_reviewed"]

    @model_validator(mode="after")
    def complete(self) -> EvalCase:
        if self.environment == "fixed" and not self.assets:
            raise ValueError("Fixed cases need frozen assets")
        if self.environment == "live" and not self.live_urls:
            raise ValueError("Live cases need source leads")
        if len({r.id for r in self.requirements}) != len(self.requirements):
            raise ValueError("Requirement IDs must be unique")
        known = {a.id for a in self.assets}
        if any(
            a.available_from_turn >= len(self.turns)
            or (a.replaces_asset_id is not None and a.replaces_asset_id not in known)
            for a in self.assets
        ):
            raise ValueError("Invalid scheduled material")
        if any(not set(r.source_ids) <= known for r in self.requirements):
            raise ValueError("Unknown requirement source IDs")
        return self

    def agent_input(self) -> dict[str, object]:
        """Only this projection may be passed to the agent."""
        return {
            "id": self.id,
            "turns": [t.model_dump() for t in self.turns],
            "cutoff": self.cutoff,
            "assets": [a.model_dump(exclude={"sha256", "family"}) for a in self.assets],
            "live_urls": self.live_urls,
        }


class Observation(Record):
    id: str
    parent_id: str | None = None
    name: str
    kind: str
    started_at: str
    duration_ms: float = 0
    status: Literal["running", "ok", "error", "cancelled", "denied"] = "running"
    input: Any = None
    output: Any = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] | None = None


class MetricResult(Record):
    name: str
    value: float | None
    numerator: float | None = None
    denominator: float | None = None
    status: Literal["scored", "not_applicable", "unjudged"] = "scored"
    source: Literal["rule", "judge", "combined", "measurement"] = "rule"
    explanation: str = ""
    references: list[str] = Field(default_factory=list)


class RunArtifact(Record):
    schema_version: Literal[1] = 1
    run_id: str
    case_id: str
    dataset_version: str
    repetition: int = 1
    trace_id: str
    status: Literal["running", "completed", "failed", "timeout", "cancelled"] = "running"
    started_at: str
    elapsed_ms: float = 0
    first_content_ms: float | None = None
    answer: str = ""
    artifacts: dict[str, str] = Field(default_factory=dict)
    artifact_metadata: dict[str, dict[str, Any]] = Field(default_factory=dict)
    observations: list[Observation] = Field(default_factory=list)
    trace_blobs: dict[str, Any] = Field(default_factory=dict)
    research_state: dict[str, Any] = Field(default_factory=dict)
    sources: dict[str, str] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    scores: list[MetricResult] = Field(default_factory=list)
    judge_results: dict[str, Any] | None = None
    judge_usage: list[dict[str, Any]] = Field(default_factory=list)
    judge_attempts: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    upload_status: Literal["local", "pending", "uploaded", "failed"] = "local"
    calibrated: bool = False
