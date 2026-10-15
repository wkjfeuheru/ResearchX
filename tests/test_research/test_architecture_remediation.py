"""Acceptance checks for the research-workbench architecture remediation."""

from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from researchx.contracts.models import Amount, FactBasis, Reference, comparable_facts
from researchx.contracts.research import (
    FinancialObservation,
    InputBundle,
    ModelRun,
    ObservationBasis,
    ResearchSpec,
    SourceSpan,
    assert_comparable,
    certificate_from_checks,
    completeness_for,
    require_production_contract,
    unbound_business_numbers,
)
from researchx.state.business_coverage import evaluate_coverage
from researchx.state.models import Evidence, ResearchMemory, SourceRecord, SourceUsage
from researchx.storage.legacy_cleanup import cleanup_registered_files


def _basis(**changes: object) -> FactBasis:
    values: dict[str, object] = {
        "security_code": "300750",
        "market": "SZSE",
        "period_start": date(2025, 1, 1),
        "period_end": date(2025, 12, 31),
        "period_type": "annual",
        "scope": "consolidated",
        "currency": "CNY",
        "unit": "亿元",
        "adjustment": "as_reported",
        "value_kind": "actual",
        "metric_id": "revenue",
        "as_of": datetime(2026, 4, 1, tzinfo=timezone.utc),
        "published_at": datetime(2026, 3, 1, tzinfo=timezone.utc),
    }
    values.update(changes)
    return FactBasis.model_validate(values)


def _spec(**changes: object) -> ResearchSpec:
    values: dict[str, object] = {
        "revision": 1,
        "kind": "financial-commentary",
        "subject": {"type": "security", "market": "SZSE", "code": "300750"},
        "as_of": "2026-04-01T00:00:00+08:00",
        "periods": ["FY2025", "FY2024"],
        "required_metrics": ["revenue", "parent_net_profit", "operating_cash_flow"],
        "requirements": [
            {"id": "profit_growth_quality", "rule": "earnings_quality", "question": "利润增长是否受到非经常性损益影响"}
        ],
        "deliverables": ["report"],
    }
    values.update(changes)
    return ResearchSpec.model_validate(values)


def _memory(*pairs: tuple[str, FactBasis]) -> ResearchMemory:
    memory = ResearchMemory(session_id="coverage")
    for identity, basis in pairs:
        source = SourceRecord(
            id=identity,
            kind="web",
            title=f"{basis.period_end.year}年度报告",
            locator=f"https://www.szse.cn/report/{identity}",
            origin_id=identity,
            collected_at=datetime(2026, 3, 2, tzinfo=timezone.utc).isoformat(),
            published_at=basis.published_at.isoformat() if basis.published_at else None,
            content_hash="a" * 64,
            snapshot="sha256:" + "a" * 64,
            usage=SourceUsage(
                category="public",
                licence_status="pending",
                model_use_allowed=True,
                internal_export_allowed=True,
                completeness="full",
            ),
        )
        memory.sources[identity] = source
        memory.evidence_pool[identity] = Evidence(
            id=identity,
            statement="营业收入",
            source_id=identity,
            collected_at=source.collected_at,
            status="source_checked",
            fact_basis=basis,
        )
    return memory


def test_coverage_rejects_wrong_company_metric_period_and_mixed_basis():
    spec = _spec()
    other = _memory(("b", _basis(security_code="000001", market="SZSE")))
    assert evaluate_coverage(other, "financial_commentary", "财报点评", spec=spec).status != "complete"
    wrong_metric = _memory(("m", _basis(metric_id="net_profit")))
    wrong_metric.evidence_pool["m"].statement = "无关陈述"
    assert any("目标指标" in gap for gap in evaluate_coverage(wrong_metric, "financial_commentary", "财报", spec=spec).gaps)
    mixed = _memory(
        ("ytd", _basis(period_type="year_to_date", metric_id="revenue")),
        ("q", _basis(period_type="single_quarter", metric_id="revenue")),
    )
    assert any("累计与单季" in gap for gap in evaluate_coverage(mixed, "financial_commentary", "财报", spec=spec).gaps)


def test_future_disclosure_and_cross_metric_comparison_are_rejected():
    future = _basis(
        as_of=datetime(2026, 6, 1, tzinfo=timezone.utc),
        published_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    coverage = evaluate_coverage(_memory(("f", future)), "financial_commentary", "财报", spec=_spec())
    assert coverage.status != "complete"
    assert any("截止" in gap for gap in coverage.gaps)
    with pytest.raises(ValueError, match="指标"):
        assert_comparable("revenue", _basis(), "parent_net_profit", _basis(metric_id="parent_net_profit"))
    assert not comparable_facts(_basis(), _basis(metric_id="parent_net_profit"))


def test_production_entry_rejects_missing_version_basis_and_tampered_observation():
    with pytest.raises(ValueError, match="v1/v2"):
        require_production_contract({"kind": "financial", "periods": []})
    with pytest.raises(ValueError, match="财务口径"):
        require_production_contract(
            {
                "schema_version": 3,
                "kind": "financial",
                "periods": [{"label": "FY", "items": {"revenue": {"value": "1", "unit": "元", "references": []}}}],
            }
        )
    basis = ObservationBasis.model_validate(_basis().model_dump(mode="json"))
    span = SourceSpan(source_id="src", source_version="sha256:" + "a" * 64, excerpt="营业收入 1 亿元")
    original = FinancialObservation(
        id="obs_revenue",
        metric_id="revenue",
        value=Decimal("1"),
        basis=basis,
        evidence_id="ev_revenue",
        source_span=span,
    )
    bundle = InputBundle(bundle_id="bundle", spec_revision=1, observations=[original]).freeze()
    changed = original.model_copy(update={"value": Decimal("2")})
    tampered = bundle.model_copy(update={"observations": [changed]})
    with pytest.raises(ValueError, match="不一致"):
        tampered.verify()


def test_unbound_numbers_and_partial_certificate_are_not_a_complete_delivery():
    gaps = unbound_business_numbers("截至2026-04-01，代码300750，收入12.5亿元。", set())
    assert gaps == ["12.5亿元"]
    certificate = certificate_from_checks(
        "artifact_1@1",
        execution_state="succeeded",
        checks={
            "scope": "passed",
            "financial_basis": "passed",
            "numeric_consistency": "passed",
            "source_checks": "passed",
            "claim_bindings": "passed",
            "requirement_coverage": "failed",
        },
        gaps=["缺少非经常性损益对应证据"],
    )
    assert certificate.completeness == "partial"
    assert completeness_for({"source_checks": "blocked"}) == "blocked"
    approved = certificate.model_dump()
    approved["review_state"] = "approved"
    with pytest.raises(ValidationError, match="部分成果"):
        type(certificate).model_validate(approved)


def test_recalculation_keeps_values_and_uses_a_new_run():
    left = ModelRun(
        model_run_id="mr_1",
        spec_revision=1,
        skill_id="earnings-forecast",
        input_observation_versions=["obs_revenue@1"],
        assumption_set_id="assumptions_1@1",
        code_bundle_hash="sha256:" + "1" * 64,
        environment_hash="sha256:" + "2" * 64,
        output_hash="sha256:" + "3" * 64,
        execution_state="succeeded",
    )
    right = left.model_copy(update={"model_run_id": "mr_2"})
    assert left.output_hash == right.output_hash
    assert left.model_run_id != right.model_run_id


def test_cleanup_deletes_only_registered_files_inside_the_root(tmp_path: Path):
    managed = tmp_path / "reports"
    managed.mkdir()
    report = managed / "draft.md"
    report.write_text("old", encoding="utf-8")
    removed = cleanup_registered_files(tmp_path, ["reports/draft.md", "../do-not-delete-researchx.txt"])
    assert removed == ["reports/draft.md"]
    assert not report.exists()
    assert cleanup_registered_files(tmp_path, ["reports/draft.md"]) == []


def test_claim_bindings_reject_a_forecast_without_a_model_run():
    from researchx.contracts.research import bind_claims

    assert bind_claims("forecast", observation_ids=[], conclusion_id="", model_run_id="")
    assert bind_claims("fact", observation_ids=["obs_1"], conclusion_id="", model_run_id="") == ""


def test_amount_missing_value_cannot_be_zero():
    with pytest.raises(ValidationError):
        Amount(value=None, missing_reason="")
    amount = Amount(
        value=None,
        missing_reason="未披露",
        references=[Reference(locator="https://www.szse.cn/a", title="年报", evidence_id="ev")],
    )
    assert amount.value is None


def test_generic_research_does_not_require_a_plan():
    spec = _spec(kind="generic-research", required_metrics=[], deliverables=[], requirements=[])
    assert spec.kind == "generic-research"
    assert json.loads(spec.model_dump_json())["kind"] == "generic-research"


def _observation(**changes: object) -> FinancialObservation:
    values: dict[str, object] = {
        "id": "obs_revenue",
        "metric_id": "revenue",
        "value": Decimal("12.5"),
        "basis": ObservationBasis.model_validate(_basis().model_dump()),
        "evidence_id": "ev_revenue",
        "source_span": {"source_id": "src_annual", "source_version": "1"},
    }
    values.update(changes)
    return FinancialObservation.model_validate(values)


def test_reuse_rechecks_security_cutoff_and_permission():
    from researchx.contracts.research import reuse_gap

    observation = _observation()
    as_of = datetime(2026, 4, 1, tzinfo=timezone.utc)
    assert reuse_gap(observation, security_code="300750", as_of=as_of, model_use_allowed=True) == ""
    assert "证券" in reuse_gap(observation, security_code="000001", as_of=as_of, model_use_allowed=True)
    earlier = datetime(2026, 2, 1, tzinfo=timezone.utc)
    assert "截止" in reuse_gap(observation, security_code="300750", as_of=earlier, model_use_allowed=True)
    assert "权限" in reuse_gap(observation, security_code="300750", as_of=as_of, model_use_allowed=False)


async def test_pause_unfinished_runs_leaves_completed_work(tmp_path: Path):
    from researchx.state.store import ResearchStore
    from researchx.storage.database import current_database
    from researchx.storage.run_records import (
        pause_unfinished_runs,
        prompt_status,
        record_accepted_run,
        record_prompt,
        run_status,
    )

    store = ResearchStore(tmp_path, "c" * 12)
    await store.load()
    async with current_database().transaction() as db:
        await record_accepted_run(
            db,
            workspace_id=store.workspace_id,
            session_id=store.session_id,
            run_id="run-open",
            request_id="run-open",
            status="running",
        )
        await record_accepted_run(
            db,
            workspace_id=store.workspace_id,
            session_id=store.session_id,
            run_id="run-done",
            request_id="run-done",
            status="completed",
        )
    await record_prompt(
        workspace_id=store.workspace_id,
        prompt_id="prompt-1",
        run_id="run-open",
        kind="permission",
    )
    assert await pause_unfinished_runs(store.workspace_id) == 1
    assert await run_status(store.workspace_id, "run-open") == "paused"
    assert await run_status(store.workspace_id, "run-done") == "completed"
    assert await prompt_status(store.workspace_id, "prompt-1") == "expired"
    assert await pause_unfinished_runs(store.workspace_id) == 0


async def test_deleting_a_chat_keeps_workspace_observations(tmp_path: Path):
    from researchx.research_application import ResearchApplication
    from researchx.state.store import ResearchStore
    from researchx.storage.conversations import ConversationRecords
    from researchx.storage.database import current_database
    from researchx.storage.run_records import (
        bind_asset,
        bind_conversation,
        conversation_project,
        observation_exists,
        project_can_cite,
        save_observation,
    )

    left, right = "a" * 12, "b" * 12
    store = ResearchStore(tmp_path, left)
    await store.load()
    other = ResearchStore(tmp_path, right)
    await other.load()
    observation = _observation()
    await save_observation(
        store.workspace_id,
        observation.id,
        version=observation.version,
        content_hash=observation.content_digest(),
        payload=observation.model_dump(mode="json"),
    )
    await bind_asset(store.workspace_id, "project-1", observation.id, "observation")
    await bind_conversation(store.workspace_id, left, "project-1")
    await bind_conversation(store.workspace_id, right, "project-1")
    assert await project_can_cite(store.workspace_id, "project-1", observation.id)
    assert await project_can_cite(store.workspace_id, "project-2", observation.id) is False
    view = await ResearchApplication(store).get_control_view("project-1")
    assert view["query_count"] <= 4
    assert "messages" not in view
    conversations = ConversationRecords(current_database(), str(tmp_path))
    await conversations.write(
        {"session_id": left, "cwd": str(tmp_path), "messages": [],
         "created_at": 1.0, "updated_at": 1.0}, channel="web"
    )
    await conversations.delete(left)
    assert await observation_exists(store.workspace_id, observation.id, observation.version)
    assert await project_can_cite(store.workspace_id, "project-1", observation.id)
    assert await conversation_project(store.workspace_id, left) is None
    assert await conversation_project(store.workspace_id, right) == "project-1"
