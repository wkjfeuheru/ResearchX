"""Deterministic tool workflows with audited fixtures, upstream reuse and report checks."""

import json
from decimal import Decimal
from importlib import import_module
from pathlib import Path
import subprocess
import sys

import pytest

from researchx.config.settings import Settings
from researchx.tools.base import ToolExecutionContext
from researchx.tools.file_read_tool import FileReadTool, FileReadToolInput
from researchx.tools.skill_tool import SkillTool, SkillToolInput
from tests.research_skill_support import FUNCTIONS, RESULT_TYPES

FIXTURES = Path(__file__).parents[1] / "fixtures/research_skills"
FORECAST = "researchx.skills.bundled.earnings-forecast.scripts"
DEEP = "researchx.skills.bundled.deep-investment-report.scripts"
REPORTING = import_module("researchx.skills._shared.reporting")


def fixture(kind):
    return json.loads((FIXTURES / f"{kind}.json").read_text())


def compute(kind):
    return FUNCTIONS[kind](RESULT_TYPES[kind].model_validate(fixture(kind)))


def test_forecast_cli_initializes_package_before_relative_imports(tmp_path):
    script = Path(import_module(FORECAST + ".forecast").__file__)
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "--input" in result.stdout and "--output" in result.stdout


@pytest.mark.asyncio
async def test_financial_tool_reference_computation_and_structured_provenance(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("RESEARCHX_CONFIG_DIR", str(tmp_path / "config"))
    context = ToolExecutionContext(cwd=tmp_path, metadata={"skill_settings": Settings()})
    entry = await SkillTool().execute(SkillToolInput(name="financial-statement-analysis"), context)
    assert not entry.is_error
    base = Path(entry.metadata["skill_base_dir"])
    reference = await FileReadTool().execute(
        FileReadToolInput(path=str(base / "references/accounting.md")), context
    )
    assert not reference.is_error and "会计取数与计算规范" in reference.output
    output = tmp_path / "financial.json"
    executed = subprocess.run(
        [
            sys.executable,
            str(base / "scripts/analyze_statements.py"),
            "--input",
            str(FIXTURES / "financial.json"),
            "--output",
            str(output),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert executed.returncode == 0, executed.stderr
    data = json.loads(output.read_text())
    assert data["status"] == "complete"
    assert data["periods"][0]["scope"] == "consolidated"
    assert data["periods"][0]["currency"] == "CNY"
    assert data["periods"][0]["items"]["revenue"]["references"][0]["page"] == 1
    assert Decimal(data["ratios"][0]["value"]) == Decimal(".4")
    assert data["ratios"][0]["formula"]


@pytest.mark.asyncio
async def test_deep_tool_calls_forecast_then_assembles_exact_upstream_results(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("RESEARCHX_CONFIG_DIR", str(tmp_path / "config"))
    context = ToolExecutionContext(cwd=tmp_path, metadata={"skill_settings": Settings()})
    names = [
        "deep-investment-report",
        "financial-statement-analysis",
        "company-event-monitor",
        "research-report-digest",
        "earnings-forecast",
    ]
    for name in names:
        result = await SkillTool().execute(SkillToolInput(name=name), context)
        assert not result.is_error
    forecast_module = import_module(FORECAST + ".forecast")
    models = import_module(FORECAST + ".models")
    report = models.DeepResult.model_validate(fixture("deep"))
    forecast = forecast_module.calculate_forecast(report.model_copy(deep=True))
    # Forecasting is independently usable without report chapters.
    standalone = report.model_copy(deep=True)
    standalone.sections = []
    assert forecast_module.calculate_forecast(standalone).status == "complete"
    analyses = [
        compute(kind).model_dump(mode="json") for kind in ("financial", "monitor", "digest")
    ]
    assembly = import_module(DEEP + ".assemble_report")
    monkeypatch.setattr(
        forecast_module,
        "project_year",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("report assembly must not recalculate forecasts")
        ),
    )
    final = assembly.assemble_report(report, forecast, analyses)
    assert final.status == "complete", final.gaps
    assert final.forecasts == forecast.forecasts and final.sensitivity == forecast.sensitivity
    assert final.base_revenue.references == forecast.base_revenue.references
    exports = import_module(DEEP + ".export_report").export_result(final, tmp_path / "exports")
    markdown = (tmp_path / "exports/deep.md").read_text()
    assert (
        len(exports["files"]) == 4
        and "1100000" in markdown
        and "fixture:annual-report.pdf" in markdown
    )
    mismatch = final.model_copy(deep=True)
    mismatch.forecasts[0]["values"]["revenue"] += 1
    with pytest.raises(ValueError, match="上游计算不一致"):
        assembly.assemble_report(mismatch, forecast, analyses)
    mismatch = final.model_copy(deep=True)
    mismatch.base_currency = "USD"
    with pytest.raises(ValueError, match="base_currency"):
        assembly.assemble_report(mismatch, forecast, analyses)


@pytest.mark.parametrize(
    "name", ["financial-commentary", "industry-commentary", "industry-deep-dive"]
)
def test_report_templates_are_runnable_and_missing_evidence_is_a_gap(name, tmp_path):
    scripts = f"researchx.skills.bundled.{name}.scripts"
    model = import_module(scripts + ".models").ReportResult
    ref = compute("financial").inputs[0].model_dump(mode="json")
    result = model.model_validate(
        {
            "kind": name,
            "subject": "测试行业或公司",
            "as_of": "2026-10-05T12:00:00+08:00",
            "inputs": [ref],
            "sections": [
                {
                    "key": "summary",
                    "title": "摘要",
                    "paragraphs": [{"text": "资料仅覆盖已披露范围。", "references": [ref]}],
                }
            ],
        }
    )
    result = REPORTING.validate_report(result)
    exported = import_module(scripts + ".export_report").export_result(result, tmp_path / name)
    markdown = (tmp_path / name / f"{name}.md").read_text()
    assert result.status == "partial" and "缺口" in markdown and "[R1]" in markdown
    assert len(result.sections) == len(REPORTING.SECTIONS[name]) and len(exported["files"]) == 4
    assert "fixture:" in markdown and "可定位证据" in markdown
    schema = json.loads(
        (
            Path(import_module(scripts + ".models").__file__).parents[1]
            / "templates/input.schema.json"
        ).read_text()
    )
    assert schema == model.model_json_schema()
    blocked = model(subject="测试", as_of="2026-10-05T12:00:00+08:00")
    assert REPORTING.validate_report(blocked).status == "blocked"


def financial_commentary(tmp_path):
    model = import_module(
        "researchx.skills.bundled.financial-commentary.scripts.models"
    ).ReportResult
    data = compute("financial").model_dump(mode="json")
    upstream = tmp_path / "financial.json"
    upstream.write_text(json.dumps(data))
    period = data["periods"][0]
    amount = period["items"]["revenue"]
    ref = amount["references"][0]
    sections = [
        {
            "key": key,
            "title": title,
            "paragraphs": [{"text": "原文中的事实和解释分别呈现。", "references": [ref]}],
        }
        for key, title in REPORTING.SECTIONS["financial-commentary"].items()
    ]
    return model.model_validate(
        {
            "subject": "样例制造",
            "as_of": data["as_of"],
            "inputs": [ref],
            "upstream_results": {"financial": str(upstream)},
            "sections": sections,
            "metrics": [
                {
                    "label": "收入",
                    "upstream": "financial",
                    "value": amount["value"],
                    "unit": amount["unit"],
                    "period": period["label"],
                    "scope": period["scope"],
                    "currency": period["currency"],
                    "references": amount["references"],
                    "pointers": {
                        "value": "/periods/0/items/revenue/value",
                        "unit": "/periods/0/items/revenue/unit",
                        "references": "/periods/0/items/revenue/references",
                        "period": "/periods/0/label",
                        "scope": "/periods/0/scope",
                        "currency": "/periods/0/currency",
                    },
                }
            ],
        }
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("value", "999"),
        ("unit", "元"),
        ("period", "2024年"),
        ("scope", "parent"),
        ("currency", "USD"),
    ],
)
def test_report_numeric_and_context_mismatch_rejected(key, value, tmp_path):
    result = financial_commentary(tmp_path)
    assert REPORTING.validate_report(result.model_copy(deep=True)).status == "complete"
    setattr(result.metrics[0], key, Decimal(value) if key == "value" else value)
    with pytest.raises(ValueError, match="上游结果不一致"):
        REPORTING.validate_report(result)


def test_report_evidence_mismatch_and_missing_numeric_context_rejected(tmp_path):
    result = financial_commentary(tmp_path)
    result.metrics[0].references[0].locator = "fabricated:source"
    with pytest.raises(ValueError, match="references"):
        REPORTING.validate_report(result)
    result = financial_commentary(tmp_path)
    del result.metrics[0].pointers["period"]
    with pytest.raises(ValueError, match="字段路径"):
        REPORTING.validate_report(result)


def test_forecast_new_and_legacy_results_and_exception_boundaries():
    forecast = import_module(FORECAST + ".forecast")
    model = import_module(FORECAST + ".models").DeepResult
    expected = compute("deep")
    assert (
        forecast.calculate_forecast(model.model_validate(fixture("deep"))).model_dump()
        == expected.model_dump()
    )
    for key, value in [("growth", "-1.01"), ("gross_margin", "1.01"), ("selling_rate", "-.01")]:
        data = fixture("deep")
        data["scenarios"][0]["years"][0]["assumptions"][key]["value"] = value
        with pytest.raises(ValueError):
            forecast.calculate_forecast(model.model_validate(data))
    data = fixture("deep")
    data["scenarios"][0]["years"][0]["year"] = 2040
    with pytest.raises(ValueError, match="连续"):
        forecast.calculate_forecast(model.model_validate(data))


def test_commentary_accepts_existing_dimensionless_ratio_contract(tmp_path):
    result = financial_commentary(tmp_path)
    data = json.loads(Path(result.upstream_results["financial"]).read_text())
    ratio = data["ratios"][0]
    metric = result.metrics[0].model_copy(deep=True)
    metric.label = "毛利率"
    metric.value = Decimal(ratio["value"])
    metric.unit = "ratio"
    metric.references = [
        type(metric.references[0]).model_validate(ref) for ref in ratio["references"]
    ]
    metric.pointers["value"] = "/ratios/0/value"
    metric.pointers["references"] = "/ratios/0/references"
    del metric.pointers["unit"]
    result.metrics.append(metric)
    assert REPORTING.validate_report(result).metrics[-1].value == Decimal(".4")
    metric.unit = "%"
    with pytest.raises(ValueError, match="unit"):
        REPORTING.validate_report(result)


@pytest.mark.asyncio
async def test_industry_commentary_tool_template_cli_and_evidence_gaps(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCHX_CONFIG_DIR", str(tmp_path / "config"))
    context = ToolExecutionContext(cwd=tmp_path, metadata={"skill_settings": Settings()})
    entry = await SkillTool().execute(SkillToolInput(name="industry-commentary"), context)
    assert not entry.is_error
    base = Path(entry.metadata["skill_base_dir"])
    template = await FileReadTool().execute(
        FileReadToolInput(path=str(base / "templates/report.md")), context
    )
    assert not template.is_error and "{{sections}}" in template.output
    ref = compute("financial").inputs[0].model_dump(mode="json")
    input_path = tmp_path / "input.json"
    input_path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "subject": "制造业（测试资料范围）",
                "as_of": "2026-10-05T12:00:00+08:00",
                "inputs": [ref],
                "sections": [
                    {
                        "key": "summary",
                        "title": "摘要",
                        "paragraphs": [
                            {
                                "text": "仅获得样例公司年度资料，行业证据尚不充分。",
                                "references": [ref],
                            }
                        ],
                    }
                ],
            }
        )
    )
    output = tmp_path / "checked.json"
    for script, arguments in (
        ("validate_report.py", ["--input", str(input_path), "--output", str(output)]),
        ("export_report.py", ["--input", str(output), "--output-dir", str(tmp_path / "report")]),
    ):
        executed = subprocess.run(
            [sys.executable, str(base / "scripts" / script), *arguments],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert executed.returncode == 0, executed.stderr
    data = json.loads(output.read_text())
    text = (tmp_path / "report/industry-commentary.md").read_text()
    assert data["status"] == "partial" and "政策与事实: 缺少可定位证据" in text
    assert "供需与价格: 缺少可定位证据" in text and "fixture:annual-report.pdf" in text
