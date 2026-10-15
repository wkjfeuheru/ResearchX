"""Report-package contracts and evidence/number checks; no analysis calculations."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import Field, field_validator
from researchx.contracts import Claim, Contract, Reference
from researchx.workspace.exports import ReportContext, export_result

SECTIONS = {
    "financial-commentary": {
        "summary": "业绩摘要",
        "performance": "业绩与变化",
        "quality": "盈利与现金流质量",
        "outlook": "展望与假设",
        "risks": "风险",
        "sources": "资料说明",
    },
    "industry-commentary": {
        "summary": "事件与核心判断",
        "policy": "政策与事实",
        "supply_demand": "供需与价格",
        "competition": "竞争格局",
        "impact": "影响链条",
        "risks": "风险",
        "sources": "资料说明",
    },
    "industry-deep-dive": {
        "summary": "摘要",
        "boundary": "行业定义与边界",
        "drivers": "行业空间与驱动",
        "chain": "产业链",
        "competition": "竞争格局",
        "outlook": "情景与催化",
        "risks": "风险",
        "sources": "资料说明",
    },
}


class Section(Contract):
    key: str
    title: str
    paragraphs: list[Claim] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class Metric(Contract):
    """Quote an upstream value with exact contextual field pointers (JSON Pointer)."""

    label: str
    upstream: str
    pointers: dict[str, str]
    value: Decimal | None
    unit: str
    period: str
    scope: str
    currency: str | None = None
    references: list[Reference] = Field(min_length=1)
    missing_reason: str = ""

    @field_validator("value")
    @classmethod
    def finite(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("报告数值必须有限")
        return value


class ReportDocument(Contract):
    schema_version: Literal[3] = 3
    kind: Literal["financial-commentary", "industry-commentary", "industry-deep-dive"]
    subject: str = Field(min_length=1)
    as_of: datetime
    inputs: list[Reference] = Field(default_factory=list)
    upstream_results: dict[str, str] = Field(default_factory=dict)
    sections: list[Section] = Field(default_factory=list)
    metrics: list[Metric] = Field(default_factory=list)
    status: Literal["complete", "partial", "blocked"] = "partial"
    gaps: list[str] = Field(default_factory=list)

    @field_validator("as_of")
    @classmethod
    def timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("as_of requires a timezone")
        return value


def pointer(data: object, path: str) -> object:
    if not path.startswith("/"):
        raise ValueError("结果字段须使用绝对 JSON Pointer")
    for key in path[1:].split("/"):
        key = key.replace("~1", "/").replace("~0", "~")
        if isinstance(data, list):
            data = data[int(key)]
        elif isinstance(data, dict):
            data = data[key]
        else:
            raise ValueError("JSON Pointer must traverse an object or array")
    return data


ReportT = TypeVar("ReportT", bound=ReportDocument)


def validate_report(result: ReportT) -> ReportT:
    """Verify selected upstream fields; propagate every uncovered section as a gap."""
    required = SECTIONS[result.kind]
    keys = [section.key for section in result.sections]
    if len(keys) != len(set(keys)) or set(keys) - set(required):
        raise ValueError("报告章节重复或不属于所选报告类型")
    upstream = {
        name: json.loads(Path(path).read_text(encoding="utf-8"))
        for name, path in result.upstream_results.items()
    }
    for name, data in upstream.items():
        result.gaps.extend(f"{name}: {gap}" for gap in data.get("gaps", []))
        if data.get("status") != "complete":
            result.gaps.append(f"{name}: 上游结果未完整核验")
    for metric in result.metrics:
        if metric.upstream not in upstream:
            raise ValueError("关键数字缺少上游结构化结果")
        required_context = {"value", "unit", "period", "scope", "references"}
        if metric.currency is not None:
            required_context.add("currency")
        # Existing financial ratios are dimensionless and intentionally have no
        # unit field. Adapt presentation without changing that result contract.
        ratio = (
            upstream[metric.upstream].get("kind") == "financial"
            and metric.pointers.get("value", "").startswith("/ratios/")
            and metric.pointers.get("value", "").endswith("/value")
        )
        pointer_keys = required_context - {"unit"} if ratio else required_context
        if not pointer_keys <= metric.pointers.keys():
            raise ValueError("数字核验必须包含值、单位、期间、口径和引用的字段路径")
        for key in required_context:
            expected = (
                "ratio"
                if key == "unit" and ratio and key not in metric.pointers
                else pointer(upstream[metric.upstream], metric.pointers[key])
            )
            actual = getattr(metric, key)
            if key == "value" and expected is not None:
                expected = Decimal(str(expected))
            elif key == "references":
                actual = [ref.model_dump(mode="json") for ref in actual]
            if actual != expected:
                raise ValueError(f"{metric.label}: {key} 与上游结果不一致")
        if metric.value is None:
            if not metric.missing_reason:
                raise ValueError("缺失数值须保留缺失原因")
            result.gaps.append(f"{metric.label}: {metric.missing_reason}")
    by_key = {section.key: section for section in result.sections}
    result.sections = []
    for key, title in required.items():
        section = by_key.get(key, Section(key=key, title=title))
        if not section.paragraphs:
            section.gaps = list(dict.fromkeys([*section.gaps, f"{title}: 缺少可定位证据"]))
        result.gaps.extend(section.gaps)
        result.sections.append(section)
    if not result.inputs:
        result.gaps.append("未提供可定位原始资料")
    if result.kind == "financial-commentary" and not upstream:
        result.gaps.append("缺少 financial-statement-analysis 结构化结果")
    # Check literal claims separately by human/Agent against cited text; this
    # validator guarantees typed metrics, context and rendered numbers only.
    result.gaps = list(dict.fromkeys(result.gaps))
    has_evidence = bool(result.inputs or any(section.paragraphs for section in result.sections))
    result.status = "blocked" if not has_evidence else "partial" if result.gaps else "complete"
    return result


def export_report(
    result: ReportDocument,
    directory: Path,
    session_directory: Path | None = None,
    task_id: str | None = None,
    *,
    template: Path,
) -> dict[str, object]:
    """Validate again and render only the requested report template."""
    result = validate_report(result)

    def render(data: dict[str, Any], session_directory: Path | None = None) -> str:
        context = ReportContext(data, session_directory)
        sections = []
        for section in data["sections"]:
            sections.append("## " + section["title"])
            sections.extend(context.claim(claim) for claim in section["paragraphs"])
            sections.extend("缺口：" + gap for gap in section["gaps"])
        metrics = [
            "| 指标 | 数值 | 单位 | 期间 | 口径 | 币种 | 来源 |",
            "|---|---:|---|---|---|---|---|",
        ]
        for row in data["metrics"]:
            value = row["value"] if row["value"] is not None else "未知：" + row["missing_reason"]
            metrics.append(
                f"| {row['label']} | {value} | {row['unit']} | {row['period']} | {row['scope']} | {row['currency'] or '未知'} | {context.refs(row['references'])} |"
            )
        text = Path(template).read_text(encoding="utf-8")
        for key, value in {
            "subject": data["subject"],
            "as_of": data["as_of"],
            "status": data["status"],
            "sections": "\n".join(sections),
            "metrics": "\n".join(metrics),
        }.items():
            text = text.replace("{{" + key + "}}", value)
        return context.finish(text.splitlines())

    return export_result(
        result,
        directory,
        session_directory,
        task_id,
        render_markdown=render,
        sheets=("sections", "metrics", "gaps", "inputs"),
    )
