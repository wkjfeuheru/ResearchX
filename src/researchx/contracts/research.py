"""Authoritative research target, frozen observations and shared quality records.

Workspace-level identities are assigned here so later asset moves do not renumber them.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
import re
from collections.abc import Mapping
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator

from researchx.contracts.models import Contract, FactBasis, comparable_facts

SCHEMA_VERSION = 3
DEFAULT_TIMEZONE = "Asia/Shanghai"
BUSINESS_NUMBER = re.compile(
    r"(?<![\d.])(-?\d{1,3}(?:,\d{3})+|-?\d+)(?:\.(\d+))?\s*(%|元|万元|亿元|千元|百万元|十亿元|倍)?"
)
DATE_OR_CODE = re.compile(
    r"\d{4}[-年/]\d{1,2}[-月/]\d{1,2}|\b\d{6}\b|\bFY20\d{2}\b|\b20\d{2}Q[1-4]\b"
)

SkillKind = Literal[
    "financial-statement-analysis",
    "financial-commentary",
    "company-event-monitor",
    "research-report-digest",
    "earnings-forecast",
    "industry-commentary",
    "industry-deep-dive",
    "deep-investment-report",
    "generic-research",
]

# Profiles already stored on sources and deliveries stay valid aliases.
ProfileName = Literal[
    "event_commentary",
    "financial_commentary",
    "ambiguous",
    "other",
    "financial-statement-analysis",
    "financial-commentary",
    "company-event-monitor",
    "research-report-digest",
    "earnings-forecast",
    "industry-commentary",
    "industry-deep-dive",
    "deep-investment-report",
    "generic-research",
]


class SkillContract(Contract):
    kind: SkillKind
    profile: ProfileName
    requires_plan: bool = True
    required_metrics: list[str] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)


SKILL_CONTRACTS: dict[str, SkillContract] = {
    item.kind: item
    for item in (
        SkillContract(
            kind="financial-statement-analysis",
            profile="financial_commentary",
            required_metrics=["revenue", "parent_net_profit", "operating_cash_flow"],
            checks=["scope", "financial_basis", "numeric_consistency", "source_checks"],
        ),
        SkillContract(
            kind="financial-commentary",
            profile="financial-commentary",
            required_metrics=["revenue", "parent_net_profit", "operating_cash_flow"],
            checks=["scope", "financial_basis", "claim_bindings", "requirement_coverage"],
        ),
        SkillContract(
            kind="company-event-monitor",
            profile="event_commentary",
            checks=["scope", "source_checks", "requirement_coverage"],
        ),
        SkillContract(
            kind="research-report-digest",
            profile="research-report-digest",
            checks=["source_checks", "claim_bindings"],
        ),
        SkillContract(
            kind="earnings-forecast",
            profile="earnings-forecast",
            required_metrics=["revenue"],
            checks=["financial_basis", "numeric_consistency", "claim_bindings"],
        ),
        SkillContract(
            kind="industry-commentary",
            profile="industry-commentary",
            checks=["scope", "source_checks", "claim_bindings"],
        ),
        SkillContract(
            kind="industry-deep-dive",
            profile="industry-deep-dive",
            checks=["scope", "source_checks", "requirement_coverage"],
        ),
        SkillContract(
            kind="deep-investment-report",
            profile="deep-investment-report",
            checks=["scope", "financial_basis", "claim_bindings", "numeric_consistency"],
        ),
        SkillContract(
            kind="generic-research",
            profile="generic-research",
            requires_plan=False,
            checks=["source_checks"],
        ),
    )
}

PROFILE_BY_SKILL = {item.kind: item.profile for item in SKILL_CONTRACTS.values()}


class ResearchSubject(Contract):
    type: Literal["security", "industry"]
    market: Literal["SSE", "SZSE", "BSE", ""] = ""
    code: str = ""
    name: str = ""

    @model_validator(mode="after")
    def security_identity(self) -> ResearchSubject:
        if self.type == "security" and not (self.market and re.fullmatch(r"\d{6}", self.code)):
            raise ValueError("证券研究目标必须包含市场和 6 位代码")
        if self.type == "industry" and not self.name:
            raise ValueError("行业研究目标必须包含名称")
        return self


class ResearchRequirement(Contract):
    id: str = Field(min_length=1)
    rule: str = Field(min_length=1)
    question: str = Field(min_length=1)


class ResearchSpec(Contract):
    """Frozen target for one research revision. Coverage never samples the clock."""

    schema_version: Literal[1] = 1
    revision: int = Field(ge=1)
    kind: SkillKind
    subject: ResearchSubject
    as_of: datetime
    timezone: str = DEFAULT_TIMEZONE
    periods: list[str] = Field(default_factory=list)
    required_metrics: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)
    requirements: list[ResearchRequirement] = Field(default_factory=list)
    deliverables: list[Literal["report", "answer", "model"]] = Field(default_factory=list)
    request_id: str = ""
    constraints: list[str] = Field(default_factory=list)

    @field_validator("as_of")
    @classmethod
    def zoned(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("研究截止时间必须带时区")
        return value

    @model_validator(mode="after")
    def defaults_from_skill(self) -> ResearchSpec:
        contract = SKILL_CONTRACTS[self.kind]
        if not self.required_metrics:
            self.required_metrics = list(contract.required_metrics)
        if contract.requires_plan and self.kind != "generic-research" and not self.deliverables:
            self.deliverables = ["report"] if self.kind != "company-event-monitor" else ["answer"]
        ZoneInfo(self.timezone)
        return self

    def profile(self) -> str:
        return SKILL_CONTRACTS[self.kind].profile


class SourceSpan(Contract):
    source_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    page: int | None = Field(default=None, ge=1)
    section: str = ""
    excerpt: str = ""
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)


class ObservationBasis(FactBasis):
    """Financial identity separated from the research cutoff stored on ResearchSpec."""

    metric_id: str = ""


class FinancialObservation(Contract):
    id: str = Field(min_length=1)
    version: int = Field(default=1, ge=1)
    metric_id: str = Field(min_length=1)
    value: Decimal | None
    basis: ObservationBasis
    evidence_id: str = Field(min_length=1)
    source_span: SourceSpan
    missing_reason: str = ""
    formula: str = ""
    input_versions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def grounded(self) -> FinancialObservation:
        if self.value is None and not self.missing_reason:
            raise ValueError("缺失数值须说明原因，不能替换为零")
        if self.value is not None and not self.value.is_finite():
            raise ValueError("观察值必须是有限数值")
        if self.value == 0 and self.missing_reason:
            raise ValueError("缺失值不能用零代替")
        if self.basis.metric_id and self.basis.metric_id != self.metric_id:
            raise ValueError("观察口径中的指标必须与 metric_id 一致")
        if self.formula and not self.input_versions:
            raise ValueError("单位转换或派生观察值必须保留输入版本")
        return self

    def content_digest(self) -> str:
        payload = self.model_dump(mode="json", exclude={"id", "version"})
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return sha256(raw.encode()).hexdigest()

    def version_ref(self) -> str:
        return f"{self.id}@{self.version}"


class InputBundle(Contract):
    bundle_id: str
    spec_revision: int = Field(ge=1)
    observations: list[FinancialObservation] = Field(default_factory=list)
    assumption_set_id: str = ""
    content_digest: str = ""

    def freeze(self) -> InputBundle:
        digest = sha256(
            json.dumps(
                [item.content_digest() for item in self.observations] + [self.assumption_set_id],
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        return self.model_copy(update={"content_digest": digest})

    def verify(self) -> None:
        frozen = self.freeze()
        if self.content_digest and self.content_digest != frozen.content_digest:
            raise ValueError("冻结输入摘要与观察值不一致")
        seen: dict[str, str] = {}
        for item in self.observations:
            previous = seen.get(item.id)
            digest = item.content_digest()
            if previous is not None and previous != digest:
                raise ValueError("同一观察 ID 的金额或口径已被修改")
            seen[item.id] = digest


class ModelRun(Contract):
    model_run_id: str
    spec_revision: int = Field(ge=1)
    skill_id: str
    input_observation_versions: list[str] = Field(default_factory=list)
    assumption_set_id: str = ""
    code_bundle_hash: str
    environment_hash: str
    output_hash: str
    execution_state: Literal["succeeded", "failed"]
    input_digest: str = ""


class QualityCertificate(Contract):
    artifact_version_id: str
    execution_state: Literal["succeeded", "failed"]
    checks: dict[str, Literal["passed", "failed", "blocked"]]
    completeness: Literal["complete", "partial", "blocked"]
    review_state: Literal["pending", "approved", "changes_requested", "invalidated"] = "pending"
    gaps: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def derived(self) -> QualityCertificate:
        expected = completeness_for(self.checks)
        if self.completeness != expected:
            raise ValueError("完整性必须由必需检查结果推导")
        if self.review_state == "approved" and self.completeness != "complete":
            raise ValueError("部分成果不能通过批准变成完整成果")
        if self.execution_state == "failed" and self.completeness == "complete":
            raise ValueError("执行失败不能标记为完整")
        return self


def completeness_for(
    checks: Mapping[str, str],
) -> Literal["complete", "partial", "blocked"]:
    if not checks:
        return "blocked"
    if checks.get("source_checks") == "blocked" or all(value == "failed" for value in checks.values()):
        return "blocked"
    if all(value == "passed" for value in checks.values()):
        return "complete"
    return "partial"


_QUALITY_CHECKS = (
    "scope",
    "financial_basis",
    "numeric_consistency",
    "source_checks",
    "claim_bindings",
    "requirement_coverage",
)


def quality_from_coverage(
    artifact_version_id: str,
    *,
    status: str,
    gaps: list[str],
    execution_state: Literal["succeeded", "failed"] = "succeeded",
) -> QualityCertificate:
    """Delivery and completion share this derivation; callers do not invent a second status."""
    if status == "complete" and not gaps:
        checks: dict[str, Literal["passed", "failed", "blocked"]] = {
            name: "passed" for name in _QUALITY_CHECKS
        }
    elif status == "blocked" or any("权限" in gap for gap in gaps):
        checks = {name: "failed" for name in _QUALITY_CHECKS}
        checks["source_checks"] = "blocked"
    else:
        checks = {name: "passed" for name in _QUALITY_CHECKS}
        checks["requirement_coverage"] = "failed"
    return certificate_from_checks(
        artifact_version_id,
        execution_state=execution_state,
        checks=checks,
        gaps=list(gaps),
    )


def certificate_from_checks(
    artifact_version_id: str,
    *,
    execution_state: Literal["succeeded", "failed"],
    checks: dict[str, Literal["passed", "failed", "blocked"]],
    gaps: list[str],
) -> QualityCertificate:
    return QualityCertificate(
        artifact_version_id=artifact_version_id,
        execution_state=execution_state,
        checks=checks,
        completeness="blocked" if execution_state == "failed" and not any(
            value == "passed" for value in checks.values()
        ) else completeness_for(checks),
        gaps=gaps,
    )


def assert_comparable(left_metric: str, left: FactBasis, right_metric: str, right: FactBasis) -> None:
    if left_metric != right_metric or not comparable_facts(left, right):
        raise ValueError("同比比较必须同时匹配指标、证券、报表口径、期间类型、单位和调整方式")


def observation_known_at(basis: FactBasis, as_of: datetime) -> bool:
    """Disclosure and market timestamps belong to the observation, not a second fact row."""
    for timestamp in (basis.published_at, basis.observation_at, basis.benchmark_as_of):
        if timestamp is not None and timestamp > as_of:
            return False
    return True


def reuse_gap(
    observation: FinancialObservation,
    *,
    security_code: str,
    as_of: datetime,
    model_use_allowed: bool,
) -> str:
    """Cross-project reuse rechecks security, cutoff and permission before a citation."""
    if observation.basis.security_code != security_code:
        return "证券不一致，不能直接复用"
    if not observation_known_at(observation.basis, as_of):
        return "截止时间之后的数据不能复用"
    if not model_use_allowed:
        return "来源权限不足，不能复用"
    return ""


def bind_claims(kind: str, *, observation_ids: list[str], conclusion_id: str, model_run_id: str) -> str:
    if kind == "fact" and not observation_ids:
        return "事实段落未绑定观察对象或已检查证据"
    if kind == "inference" and not conclusion_id:
        return "推断段落未绑定结论和论证摘要"
    if kind == "forecast" and not model_run_id:
        return "预测段落未绑定模型运行输出"
    return ""


def unbound_business_numbers(text: str, bound_tokens: set[str]) -> list[str]:
    """Dates and security codes are not business values; unbound amounts are gaps."""
    gaps = []
    redacted = DATE_OR_CODE.sub(" ", text)
    for match in BUSINESS_NUMBER.finditer(redacted):
        token = match.group(0).strip()
        if token and token not in bound_tokens and match.group(3):
            gaps.append(token)
    return gaps


def require_production_contract(raw: object) -> None:
    """Production calculation accepts only contract v3 and never a weaker basis path."""
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("生产计算入口拒绝缺少契约版本或 v1/v2 输入")
    if raw.get("kind") == "financial":
        for period in raw.get("periods") or []:
            if not isinstance(period, dict):
                continue
            for key, amount in (period.get("items") or {}).items():
                _require_amount_basis(f"{period.get('label', '')}/{key}", amount)
    if raw.get("kind") == "deep":
        _require_amount_basis("base_revenue", raw.get("base_revenue"))


def _require_amount_basis(label: str, amount: object) -> None:
    if not isinstance(amount, dict) or amount.get("value") is None:
        return
    basis = amount.get("fact_basis")
    references = amount.get("references") or []
    if not isinstance(basis, dict) or not basis.get("unit") or not basis.get("security_code"):
        raise ValueError(f"{label}: 生产计算拒绝缺少财务口径")
    if not references or any(not isinstance(item, dict) or not item.get("evidence_id") for item in references):
        raise ValueError(f"{label}: 生产计算拒绝缺少数值引用")
