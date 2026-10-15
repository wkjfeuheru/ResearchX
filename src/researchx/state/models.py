"""Validated, auditable research records. All IDs are local to one session."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Annotated, Literal
from uuid import uuid4

from typing_extensions import TypedDict
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from researchx.contracts.models import FactBasis


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskContext(Record):
    id: str = Field(default_factory=lambda: new_id("ctx"))
    goal: str = Field(min_length=1)
    subjects: list[str] = Field(default_factory=list)
    scope: str = ""
    time_range: str = ""
    constraints: list[str] = Field(default_factory=list)
    deliverables: list[str] = Field(default_factory=list)
    user_source_ids: list[str] = Field(default_factory=list)
    supersedes: str | None = None
    created_at: str = Field(default_factory=now)


TaskStatus = Literal[
    "pending", "ready", "in_progress", "blocked", "validating", "completed", "failed", "cancelled"
]
ProjectStatus = Literal[
    "created",
    "planning",
    "running",
    "replanning",
    "suspended",
    "validating",
    "completed",
    "failed",
    "cancelled",
]


class ResearchTask(Record):
    id: str = Field(default_factory=lambda: new_id("task"))
    title: str = Field(min_length=1, max_length=120)
    status: TaskStatus = "pending"
    blocker: str = ""
    completion_note: str = ""
    started_at: str | None = None
    completed_at: str | None = None
    updated_at: str = Field(default_factory=now)
    description: str = ""
    dependencies: list[str] = Field(default_factory=list)
    dependency_revisions: dict[str, int] = Field(default_factory=dict)
    acceptance_criteria: list[str] = Field(default_factory=list)
    criterion_results: dict[str, list[str]] = Field(default_factory=dict)
    required_artifact_kinds: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    finding_ids: list[str] = Field(default_factory=list)
    requires_evidence: bool = True
    task_revision: int = Field(default=1, ge=1)
    assigned_plan_revision: int = Field(default=1, ge=1)
    failure_count: int = Field(default=0, ge=0)
    lease_id: str | None = None
    defects: list[str] = Field(default_factory=list)


class ResearchPlan(Record):
    id: str = Field(default_factory=lambda: new_id("plan"))
    context_id: str
    title: str = Field(min_length=1, max_length=120)
    tasks: list[ResearchTask] = Field(min_length=1)
    reused_evidence_ids: list[str] = Field(default_factory=list)
    supersedes: str | None = None
    archived: bool = False
    created_at: str = Field(default_factory=now)
    project_id: str | None = None
    objective_revision: int = Field(default=1, ge=1)
    revision: int = Field(default=1, ge=1)
    rationale: str = ""
    assumptions: list[str] = Field(default_factory=list)


class ResearchObjective(Record):
    project_id: str
    report_type: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    requirements: list[str] = Field(min_length=1)
    deliverables: list[str] = Field(min_length=1)
    required_sections: list[str] = Field(default_factory=list)
    revision: int = Field(default=1, ge=1)


class ResearchProject(Record):
    id: str
    # Host-owned checkpoint binding; never accepted in a model tool's input schema.
    workspace_path: str | None = None
    status: ProjectStatus = "created"
    objective_revision: int = 1
    plan_revision: int = 0
    execution_epoch: int = 0
    feedback: list[str] = Field(default_factory=list)
    planning_calls: int = 0
    planning_tokens: int = 0
    max_planning_calls: int = Field(default=8, ge=1)
    max_planning_tokens: int = Field(default=64000, ge=1)
    last_error: str = ""
    delivery_manifest: dict[str, object] = Field(default_factory=dict)


class PlanProposal(Record):
    objective_revision: int = Field(ge=1)
    tasks: list[ResearchTask] = Field(min_length=1, max_length=30)
    rationale: str = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list)


class TaskRevision(Record):
    task_id: str
    expected_revision: int = Field(ge=1)
    replacement: ResearchTask


class PlanPatch(Record):
    base_plan_revision: int = Field(ge=1)
    objective_revision: int = Field(ge=1)
    add_tasks: list[ResearchTask] = Field(default_factory=list, max_length=30)
    revise_tasks: list[TaskRevision] = Field(default_factory=list)
    cancel_task_ids: list[str] = Field(default_factory=list)
    invalidate_artifact_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)


class ResearchArtifact(Record):
    id: str = Field(default_factory=lambda: new_id("artifact"))
    task_id: str
    task_revision: int = Field(ge=1)
    plan_revision: int = Field(ge=1)
    objective_revision: int = Field(ge=1)
    execution_id: str
    kind: Literal["note", "dataset", "model", "chart", "report_draft"]
    title: str = Field(min_length=1)
    snapshot: str
    content_hash: str
    evidence_ids: list[str] = Field(default_factory=list)
    finding_ids: list[str] = Field(default_factory=list)
    input_artifact_ids: list[str] = Field(default_factory=list)
    unit: str = ""
    currency: str = ""
    period: str = ""
    reproduction: str = ""
    assumption_evidence_ids: list[str] = Field(default_factory=list)
    sections: list[str] = Field(default_factory=list)
    stale: bool = False
    stale_reason: str = ""
    file_id: str | None = None
    created_at: str = Field(default_factory=now)


class ResearchExecution(Record):
    id: str
    tool_use_id: str
    tool_name: str
    task_id: str
    task_revision: int
    plan_revision: int
    objective_revision: int
    epoch: int
    lease_id: str
    status: Literal["running", "committed", "failed", "cancelled", "rejected"] = "running"
    source_ids: list[str] = Field(default_factory=list)
    note: str = ""


class CheckResult(Record):
    check_id: str
    passed: bool
    blocking: bool = True
    message: str
    affected_task_ids: list[str] = Field(default_factory=list)


class CompletionResult(Record):
    passed: bool
    checks: list[CheckResult] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)


SourceKind = Literal["web", "search", "file", "mcp", "user", "tool", "calculation"]


class SourceUsage(Record):
    category: Literal["unclassified", "public", "licensed", "internal"] = "unclassified"
    provider: str = ""
    licence_status: Literal["unknown", "pending", "confirmed", "restricted"] = "unknown"
    model_use_allowed: bool = False
    internal_export_allowed: bool = False
    completeness: Literal["unknown", "full", "fragment", "summary"] = "unknown"


class SourceRecord(Record):
    id: str
    plan_id: str | None = None
    task_id: str | None = None
    kind: SourceKind
    title: str
    locator: str
    origin_id: str
    collected_at: str
    published_at: str | None = None
    event_at: str | None = None
    content_hash: str
    snapshot: str
    fragment: bool = False
    is_error: bool = False
    execution_id: str | None = None
    task_revision: int | None = None
    plan_revision: int | None = None
    # Missing on legacy rows: never infer a licence from a URL or a successful fetch.
    usage: SourceUsage = Field(default_factory=lambda: SourceUsage())
    original_locator: str | None = None
    corrects_source_id: str | None = None
    request_profile: Literal["event_commentary", "financial_commentary"] | None = None

    @field_validator("collected_at")
    @classmethod
    def utc_collection_time(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or (parsed.utcoffset() or timedelta(0)).total_seconds() != 0:
            raise ValueError("Source collection timestamp must be UTC")
        return value


class Evidence(Record):
    id: str = Field(default_factory=lambda: new_id("ev"))
    plan_id: str | None = None
    task_id: str | None = None
    statement: str = Field(min_length=1)
    source_id: str
    locator: str = ""
    period: str = ""
    fact_basis: FactBasis | None = None
    collected_at: str
    published_at: str | None = None
    status: Literal["pending", "source_checked", "verified", "retracted"] = "pending"
    needs_review: bool = False
    verification_step_id: str | None = None
    verification_note: str = ""
    verification_method: Literal["source", "cross_source", "calculation"] | None = None
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    input_evidence_ids: list[str] = Field(default_factory=list)
    calculation_step_id: str | None = None
    supersedes: str | None = None
    created_at: str = Field(default_factory=now)


class ReasoningStep(Record):
    id: str = Field(default_factory=lambda: new_id("step"))
    plan_id: str | None = None
    task_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    prior_step_ids: list[str] = Field(default_factory=list)
    method: str = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)
    result: str = Field(min_length=1)
    output: str = Field(min_length=1)
    uncertainty: str = ""
    verification: bool = False
    created_at: str = Field(default_factory=now)


class Conclusion(Record):
    id: str = Field(default_factory=lambda: new_id("claim"))
    plan_id: str | None = None
    statement: str = Field(min_length=1)
    status: Literal["tentative", "verified", "conflicted", "retracted"] = "tentative"
    evidence_ids: list[str] = Field(min_length=1)
    step_ids: list[str] = Field(min_length=1)
    needs_review: bool = False
    supersedes: str | None = None
    created_at: str = Field(default_factory=now)


class ResearchState(Record):
    current_plan_id: str | None = None
    current_task_id: str | None = None
    unresolved: list[str] = Field(default_factory=list)
    next_step: str = ""
    replan_required: bool = False


class ConflictSide(Record):
    statement: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    step_ids: list[str] = Field(default_factory=list)
    conclusion_ids: list[str] = Field(default_factory=list)
    subject: str = ""
    period: str = ""
    unit: str = ""
    basis: str = ""
    conditions: list[str] = Field(default_factory=list)


class SourceAssessment(Record):
    evidence_id: str
    originality: str = Field(min_length=1)
    directness: str = Field(min_length=1)
    scope_match: str = Field(min_length=1)
    timing_and_corrections: str = Field(min_length=1)
    independence: str = Field(min_length=1)
    reproducibility: str = Field(min_length=1)


class ArbitrationDecision(Record):
    outcome: Literal["prefer_side", "compatible", "conditional", "unresolved"]
    preferred_side: int | None = Field(default=None, ge=0)
    statement: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    step_ids: list[str] = Field(min_length=1)
    assessments: list[SourceAssessment] = Field(min_length=1)
    rejected_reasons: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    remaining_gaps: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def complete_outcome(self) -> ArbitrationDecision:
        if self.outcome == "prefer_side" and (
            self.preferred_side is None or not self.rejected_reasons
        ):
            raise ValueError(
                "Preferring a side requires its index and reasons for rejecting alternatives"
            )
        if self.outcome != "prefer_side" and self.preferred_side is not None:
            raise ValueError("Only prefer_side may select a preferred side")
        if self.outcome == "conditional" and not self.conditions:
            raise ValueError("Conditional conclusions require explicit conditions")
        if self.outcome == "unresolved" and not self.remaining_gaps:
            raise ValueError("Unresolved conflicts require remaining evidence gaps")
        return self


class ConflictRecord(Record):
    id: str = Field(default_factory=lambda: new_id("conflict"))
    plan_id: str
    context_id: str
    question: str = Field(min_length=1)
    kind: Literal["scope", "fact", "calculation", "interpretation"]
    core: bool = True
    sides: list[ConflictSide] = Field(min_length=2, max_length=10)
    additional_evidence_ids: list[str] = Field(default_factory=list)
    status: Literal[
        "open", "investigating", "awaiting_review", "resolved", "unresolved", "interrupted"
    ] = "open"
    current_arbitration_id: str | None = None
    last_attempt_fingerprint: str = ""
    review_note: str = ""
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)


class ArbitrationRecord(Record):
    id: str = Field(default_factory=lambda: new_id("arb"))
    conflict_id: str
    input_fingerprint: str
    review_fingerprint: str = ""
    evidence_versions: list[str]
    status: Literal[
        "running", "completed", "timeout", "budget_exhausted", "interrupted", "failed", "stale"
    ] = "running"
    report: ArbitrationDecision | None = None
    decision: ArbitrationDecision | None = None
    imported_ids: list[str] = Field(default_factory=list)
    replaced_conclusion_ids: list[str] = Field(default_factory=list)
    output_conclusion_ids: list[str] = Field(default_factory=list)
    note: str = ""
    usage: dict[str, object] = Field(default_factory=dict)
    started_at: str = Field(default_factory=now)
    finished_at: str | None = None


class OperationReceipt(TypedDict):
    fingerprint: str
    receipt: dict[str, object]


class PendingSteer(TypedDict, total=False):
    target_request_id: str
    text: str
    accepted_at: str
    ready: bool


class SourceDisplay(TypedDict):
    kind: SourceKind
    fragment: bool
    title: str
    locator: str
    published_at: str | None
    collected_at: str


class CitationSnapshot(TypedDict):
    number: int
    evidence: dict[str, object]
    source: SourceDisplay


class AnswerReceipt(TypedDict):
    answer_id: str
    model_text: str
    memory_revision: int
    rendered: str
    citations: dict[str, CitationSnapshot]
    invalid: list[str]
    created_at: str


class ResearchMemory(Record):
    schema_version: Literal[1] = 1
    session_id: str
    revision: int = Field(default=0, ge=0)
    current_context_id: str | None = None
    task_context: list[TaskContext] = Field(default_factory=list)
    research_state: ResearchState = Field(default_factory=ResearchState)
    plans: dict[str, ResearchPlan] = Field(default_factory=dict)
    sources: dict[str, SourceRecord] = Field(default_factory=dict)
    evidence_pool: dict[str, Evidence] = Field(default_factory=dict)
    reasoning_chain: dict[str, ReasoningStep] = Field(default_factory=dict)
    conclusions: dict[str, Conclusion] = Field(default_factory=dict)
    conflicts: dict[str, ConflictRecord] = Field(default_factory=dict)
    arbitrations: dict[str, ArbitrationRecord] = Field(default_factory=dict)
    history: list[dict[str, object]] = Field(default_factory=list)
    operations: dict[str, OperationReceipt] = Field(default_factory=dict)
    answers: dict[str, AnswerReceipt] = Field(default_factory=dict)
    pending_steers: dict[str, PendingSteer] = Field(default_factory=dict)
    project: ResearchProject | None = None
    objectives: dict[int, ResearchObjective] = Field(default_factory=dict)
    artifacts: dict[str, ResearchArtifact] = Field(default_factory=dict)
    executions: dict[str, ResearchExecution] = Field(default_factory=dict)


class ReadMemory(Record):
    action: Literal["read"]
    ids: list[str] = Field(default_factory=list)
    include_content: bool = False


class SearchMemory(Record):
    action: Literal["search"]
    query: str = Field(default="", max_length=200)
    record_types: list[Literal["source", "evidence", "reasoning", "conclusion", "conflict", "arbitration", "artifact"]] = Field(default_factory=list)
    task_id: str | None = None
    status: str | None = None
    cursor: str | None = None
    limit: int = Field(default=20, ge=1, le=100)


class Mutation(Record):
    operation_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)


class SetContext(Mutation):
    action: Literal["set_context"]
    goal: str = Field(min_length=1)
    subjects: list[str] = Field(default_factory=list)
    scope: str = ""
    time_range: str = ""
    constraints: list[str] = Field(default_factory=list)
    deliverables: list[str] = Field(default_factory=list)
    user_source_ids: list[str] = Field(min_length=1)


class CreatePlan(Mutation):
    action: Literal["create_plan"]
    title: str = Field(min_length=1, max_length=120)
    tasks: list[Annotated[str, Field(min_length=1, max_length=120)]] = Field(
        min_length=1, max_length=30
    )
    reused_evidence_ids: list[str] = Field(default_factory=list)


class UpdateTask(Mutation):
    action: Literal["update_task"]
    task_id: str
    status: TaskStatus
    blocker: str = ""
    completion_note: str = ""
    next_step: str = ""
    unresolved: list[str] | None = None


class AddEvidence(Mutation):
    action: Literal["add_evidence"]
    statement: str = Field(min_length=1)
    source_id: str
    locator: str = ""
    period: str = ""
    fact_basis: FactBasis | None = None
    status: Literal["pending", "retracted"] = "pending"
    verification_step_id: str | None = None
    verification_note: str = ""
    verification_method: Literal["source", "cross_source", "calculation"] | None = None
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    input_evidence_ids: list[str] = Field(default_factory=list)
    calculation_step_id: str | None = None
    supersedes: str | None = None


class AddReasoning(Mutation):
    action: Literal["add_reasoning"]
    evidence_ids: list[str] = Field(default_factory=list)
    prior_step_ids: list[str] = Field(default_factory=list)
    method: str = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)
    result: str = Field(min_length=1)
    output: str = Field(min_length=1)
    uncertainty: str = ""
    verification: bool = False

    @model_validator(mode="after")
    def has_inputs(self) -> AddReasoning:
        if not self.evidence_ids and not self.prior_step_ids:
            raise ValueError("A reasoning step needs evidence or a prior step")
        return self


class VerifyEvidence(Mutation):
    action: Literal["verify_evidence"]
    evidence_id: str
    level: Literal["source_checked", "verified"]
    method: Literal["source", "cross_source", "calculation"]
    verification_step_id: str
    verification_note: str = Field(min_length=1)
    supporting_evidence_ids: list[str] = Field(default_factory=list)


class AddConclusion(Mutation):
    action: Literal["add_conclusion"]
    statement: str = Field(min_length=1)
    status: Literal["tentative", "verified", "conflicted", "retracted"] = "tentative"
    evidence_ids: list[str] = Field(min_length=1)
    step_ids: list[str] = Field(min_length=1)
    supersedes: str | None = None


class AddConflict(Mutation):
    action: Literal["add_conflict"]
    question: str = Field(min_length=1)
    kind: Literal["scope", "fact", "calculation", "interpretation"]
    core: bool = True
    sides: list[ConflictSide] = Field(min_length=2, max_length=10)


class SubmitConflictReport(Mutation):
    action: Literal["submit_conflict_report"]
    conflict_id: str
    report: ArbitrationDecision


class ResolveConflict(Mutation):
    action: Literal["resolve_conflict"]
    conflict_id: str
    arbitration_id: str
    decision: ArbitrationDecision
    conclusion_status: Literal["tentative", "verified"] = "tentative"


class ReopenConflict(Mutation):
    action: Literal["reopen_conflict"]
    conflict_id: str
    reason: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


ResearchOperation = Annotated[
    ReadMemory
    | SearchMemory
    | SetContext
    | CreatePlan
    | UpdateTask
    | AddEvidence
    | AddReasoning
    | VerifyEvidence
    | AddConclusion
    | AddConflict
    | SubmitConflictReport
    | ResolveConflict
    | ReopenConflict,
    Field(discriminator="action"),
]


class ArtifactSubmission(Record):
    task_id: str
    task_revision: int = Field(ge=1)
    plan_revision: int = Field(ge=1)
    execution_id: str
    kind: Literal["note", "dataset", "model", "chart", "report_draft"]
    title: str = Field(min_length=1)
    content: str = Field(min_length=1, max_length=200000)
    criteria: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    finding_ids: list[str] = Field(default_factory=list)
    input_artifact_ids: list[str] = Field(default_factory=list)
    unit: str = ""
    currency: str = ""
    period: str = ""
    reproduction: str = ""
    assumption_evidence_ids: list[str] = Field(default_factory=list)
    sections: list[str] = Field(default_factory=list)
