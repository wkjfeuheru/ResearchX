"""Deterministic, versioned coverage checks for A-share non-financial drafts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal, cast
import re

from pydantic import BaseModel, Field
from researchx.contracts.models import FactBasis, comparable_facts
from researchx.contracts.research import ResearchSpec, observation_known_at

from researchx.config.sites import classify_source, get_research_sites, source_identity
from researchx.state.models import ResearchMemory, SourceRecord

PROFILE_VERSION = 1
Profile = Literal[
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

_FINANCIAL_PROFILES = {
    "financial_commentary",
    "financial-commentary",
    "financial-statement-analysis",
    "earnings-forecast",
    "deep-investment-report",
}


class CoverageChannel(BaseModel):
    name: str
    status: Literal["ok", "missing", "summary_only", "stale"]
    source_ids: list[str] = Field(default_factory=list)
    reason: str = ""
    attempts: list[dict[str, object]] = Field(default_factory=list)


class CoverageResult(BaseModel):
    profile: Profile
    version: int = PROFILE_VERSION
    status: Literal["complete", "partial", "blocked"]
    as_of: str
    window_start: str
    channels: list[CoverageChannel]
    gaps: list[str]


def _matches_subject(basis: FactBasis, spec: ResearchSpec | None) -> bool:
    if spec is None or spec.subject.type != "security":
        return True
    return basis.security_code == spec.subject.code and basis.market == spec.subject.market


def _matches_period(basis: FactBasis, spec: ResearchSpec | None) -> bool:
    if spec is None or not spec.periods:
        return True
    label = f"FY{basis.period_end.year}"
    return label in spec.periods or basis.period_end.isoformat() in spec.periods


def _matches_metric(evidence: object, spec: ResearchSpec | None) -> bool:
    if spec is None or not spec.required_metrics:
        return True
    basis = getattr(evidence, "fact_basis", None)
    metric = getattr(basis, "metric_id", "") or ""
    statement = getattr(evidence, "statement", "")
    if metric:
        return metric in spec.required_metrics
    aliases = {
        "revenue": ("营业收入", "营收"),
        "parent_net_profit": ("归母净利润", "归属于母公司"),
        "operating_cash_flow": ("经营现金流", "经营活动产生的现金流量净额"),
        "net_profit": ("净利润",),
    }
    return any(
        metric_id in spec.required_metrics and any(word in statement for word in words)
        for metric_id, words in aliases.items()
    )


def _spec_gaps(memory: ResearchMemory, spec: ResearchSpec, as_of: datetime) -> list[str]:
    gaps: list[str] = []
    if spec.subject.type == "security":
        bases = [
            evidence.fact_basis
            for evidence in memory.evidence_pool.values()
            if evidence.fact_basis is not None and evidence.status in {"source_checked", "verified"}
        ]
        foreign = [
            basis
            for basis in bases
            if basis is not None
            and (basis.security_code != spec.subject.code or basis.market != spec.subject.market)
        ]
        matched = [basis for basis in bases if basis is not None and basis not in foreign]
        if foreign and not matched:
            gaps.append("覆盖对象与研究目标证券不一致")
        future = [
            basis
            for basis in matched
            if basis is not None and not observation_known_at(basis, as_of)
        ]
        if future:
            gaps.append("截止时点之后披露或观察到的数据不能用于本次研究")
        period_types = {basis.period_type for basis in matched if basis is not None}
        if "year_to_date" in period_types and "single_quarter" in period_types:
            gaps.append("累计与单季口径不能混用")
        if spec.required_metrics:
            present = {
                basis.metric_id for basis in matched if basis is not None and basis.metric_id
            }
            missing = [metric for metric in spec.required_metrics if metric not in present]
            if missing and not any(
                _matches_metric(evidence, spec)
                for evidence in memory.evidence_pool.values()
                if evidence.fact_basis in matched
            ):
                gaps.append("缺少目标指标: " + ",".join(missing))
    return gaps


def answer_profiles(text: str, chosen: str | None) -> tuple[Profile | None, Profile | None]:
    """Return the coverage profile and the profile stored on a frozen answer."""
    inferred = classify_request(text)
    selected = chosen if inferred in {"ambiguous", "other"} else inferred
    if selected not in {"event_commentary", "financial_commentary", "ambiguous"}:
        return None, None
    profile = cast(Profile, selected)
    freeze = profile if profile in {"event_commentary", "financial_commentary"} else None
    return profile, freeze


def classify_request(text: str) -> Profile:
    finance = any(
        word in text
        for word in (
            "财报",
            "年报",
            "季报",
            "半年报",
            "财务报表",
            "盈利预测",
            "financial-commentary",
            "earnings-forecast",
            "deep-investment-report",
        )
    )
    event = any(word in text for word in ("新闻", "事件", "公告", "舆情", "最近发生"))
    if finance and event:
        return "ambiguous"
    if finance:
        return "financial_commentary"
    if event:
        return "event_commentary"
    if any(word in text for word in ("点评", "研报", "研究报告", "commentary")):
        return "ambiguous"
    return "other"


def _source_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if len(value) == 10:
        # Date-only disclosures are retained at their precision; use end of day
        # conservatively when comparing with an intraday cutoff.
        return parsed.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        raise ValueError("Disclosure timestamp needs a timezone")
    return parsed


def _usable_sources(memory: ResearchMemory, as_of: datetime) -> list[SourceRecord]:
    checked = {
        evidence.source_id
        for evidence in memory.evidence_pool.values()
        if evidence.status in {"source_checked", "verified"}
        and not evidence.needs_review
        and not any(item.supersedes == evidence.id for item in memory.evidence_pool.values())
    }
    result = []
    for source in memory.sources.values():
        if source.id not in checked or source.is_error or source.fragment:
            continue
        if not source.usage.model_use_allowed or source.usage.category not in {
            "public",
            "licensed",
        }:
            continue
        if source.usage.completeness != "full":
            continue
        if source.published_at:
            try:
                published = _source_time(source.published_at)
                if published > as_of:
                    continue
            except ValueError:
                continue
        result.append(source)
    return result


def evaluate_coverage(
    memory: ResearchMemory,
    profile: Profile,
    request: str,
    *,
    as_of: datetime | None = None,
    attempts: list[dict[str, object]] | None = None,
    spec: ResearchSpec | None = None,
) -> CoverageResult:
    # The research cutoff is fixed on the spec. Callers without a spec keep the
    # explicit timestamp they already passed; only the legacy path samples now.
    if spec is not None:
        as_of = spec.as_of
    as_of = as_of or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        raise ValueError("Coverage cutoff needs a timezone")
    days = 30
    window_gap = ""
    relative = re.search(r"(?:最近|近|过去)([一二两三四六十]|\d+)(天|周|个月|月)", request)
    if relative:
        number = relative.group(1)
        count = (
            int(number)
            if number.isdigit()
            else {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "六": 6, "十": 10}[number]
        )
        days = count * {"天": 1, "周": 7, "个月": 30, "月": 30}[relative.group(2)]
        if not 1 <= days <= 36500:
            days = 30
            window_gap = "请求的时间跨度无效，须由发起人确认"
    start = as_of - timedelta(days=days)
    dates = [
        f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        for year, month, day in re.findall(r"(\d{4})[-年/](\d{1,2})[-月/](\d{1,2})日?", request)
    ]
    if len(dates) == 1 and any(word in request for word in ("截至", "截止")):
        try:
            cutoff = datetime.fromisoformat(dates[0]).replace(tzinfo=timezone.utc) + timedelta(
                days=1
            )
            as_of = min(as_of, cutoff)
            start = as_of - timedelta(days=days)
        except (ValueError, OverflowError):
            window_gap = "资料截止日期无效，须由发起人确认"
    if len(dates) >= 2:
        try:
            start = datetime.fromisoformat(dates[0]).replace(tzinfo=timezone.utc)
            requested_end = datetime.fromisoformat(dates[1]).replace(
                tzinfo=timezone.utc
            ) + timedelta(days=1)
            if start >= requested_end:
                raise ValueError("reversed window")
            as_of = min(as_of, requested_end)
        except (ValueError, OverflowError):
            window_gap = "请求中的日期范围无效，须由发起人确认"
    sources = _usable_sources(memory, as_of)
    official, independent, financial, prices, broker = [], [], [], [], []
    for source in sources:
        classification = classify_source(source.locator, get_research_sites())
        categories = classification["categories"] or []
        in_window = False
        if source.event_at or source.published_at:
            try:
                observed = _source_time(source.event_at or source.published_at or "")
                in_window = start <= observed <= as_of
            except ValueError:
                pass
        if classification["tier"] == "official" and "disclosure" in categories:
            if in_window or profile == "financial_commentary":
                official.append(source)
            if any(word in source.title for word in ("年度报告", "半年报", "季度报告", "财务报告")):
                financial.append(source)
        elif classification["tier"] == "media" and in_window:
            independent.append(source)
        if source.kind == "mcp" and source.usage.provider == "approved_market_data":
            if any(
                evidence.source_id == source.id
                and evidence.fact_basis is not None
                and evidence.fact_basis.period_type == "point_in_time"
                and evidence.fact_basis.benchmark_code
                and evidence.fact_basis.observation_at is not None
                and evidence.fact_basis.benchmark_as_of == evidence.fact_basis.observation_at
                and start <= evidence.fact_basis.observation_at <= as_of
                for evidence in memory.evidence_pool.values()
                if evidence.status in {"source_checked", "verified"} and not evidence.needs_review
            ):
                prices.append(source)
        if source.usage.category == "licensed" and source.usage.provider == "approved_broker":
            broker.append(source)

    def channel(
        name: str, found: list[SourceRecord], *, independent_publishers: int = 1
    ) -> CoverageChannel:
        unique = {
            source_identity(item.original_locator or item.locator): item.id
            for item in reversed(found)
        }
        ids = list(unique.values())
        if len(unique) >= independent_publishers:
            return CoverageChannel(name=name, status="ok", source_ids=ids)
        return CoverageChannel(
            name=name,
            status="missing",
            source_ids=ids,
            reason="缺少指定渠道的已核对完整原文或独立原始出处",
        )

    channels: list[CoverageChannel] = []
    if profile == "event_commentary":
        official_origins = {
            source_identity(item.original_locator or item.locator) for item in official
        }
        official_hashes = {item.content_hash for item in official}
        independent = [
            item
            for item in independent
            if source_identity(item.original_locator or item.locator) not in official_origins
            and item.content_hash not in official_hashes
        ]
        channels = [
            channel("official_disclosure", official),
            channel("independent_news", independent),
        ]
        if any(word in request for word in ("股价", "涨跌", "市场表现", "跑赢")):
            channels.append(channel("market_data_and_benchmark", prices))
        if any(word in request for word in ("券商", "研报", "评级")):
            channels.append(channel("licensed_broker_view", broker))
    elif profile in _FINANCIAL_PROFILES:
        financial_ids = {item.id for item in financial}
        bases = [
            evidence.fact_basis
            for evidence in memory.evidence_pool.values()
            if evidence.source_id in financial_ids
            and evidence.status in {"source_checked", "verified"}
            and not evidence.needs_review
            and evidence.fact_basis is not None
            and evidence.fact_basis.value_kind == "actual"
            and evidence.fact_basis.unit is not None
            and evidence.fact_basis.as_of <= as_of
            and observation_known_at(evidence.fact_basis, as_of)
            and _matches_subject(evidence.fact_basis, spec)
            and _matches_period(evidence.fact_basis, spec)
            and _matches_metric(evidence, spec)
        ]
        comparable = any(
            comparable_facts(a, b) or comparable_facts(b, a)
            for i, a in enumerate(bases)
            for b in bases[i + 1 :]
        )
        channels = [
            channel("official_financial_report", financial),
            CoverageChannel(
                name="comparable_period_report",
                status="ok" if comparable else "missing",
                source_ids=[item.id for item in financial],
                reason="缺少同一证券、单位、报表范围、累计/单季和调整口径可比的正式财报证据"
                if not comparable
                else "",
            ),
        ]
    else:
        channels = [
            CoverageChannel(
                name="research_type", status="missing", reason="任务类型不明确，须由发起人确认"
            )
        ]
    gaps = [f"{item.name}: {item.reason}" for item in channels if item.status != "ok"]
    if window_gap:
        gaps.append(window_gap)
    attempt_channels = {
        "official_disclosure": {"disclosure"},
        "official_financial_report": {"disclosure"},
        "comparable_period_report": {"disclosure"},
        "independent_news": {"news", "all"},
        "licensed_broker_view": {"industry", "all"},
        "market_data_and_benchmark": {"market"},
    }
    for item in channels:
        item.attempts = [
            attempt
            for attempt in (attempts or [])
            if attempt.get("channel") in attempt_channels.get(item.name, set())
        ]
        if item.attempts:
            latest = max(
                item.attempts, key=lambda attempt: float(str(attempt.get("created_at", 0)))
            )
            if latest.get("outcome") in {"error", "partial"} or latest.get("errors"):
                item.status = "missing"
                item.reason = "渠道查询失败或不完整，现有材料不足以确认完整覆盖"
                gaps.append(f"{item.name}: {item.reason}")
    if spec is not None:
        gaps.extend(_spec_gaps(memory, spec, as_of))
    status: Literal["complete", "partial", "blocked"] = (
        "complete"
        if not gaps
        else "partial"
        if any(item.status == "ok" for item in channels)
        else "blocked"
    )
    return CoverageResult(
        profile=profile,
        status=status,
        as_of=as_of.isoformat(),
        window_start=start.isoformat(),
        channels=channels,
        gaps=gaps,
    )
