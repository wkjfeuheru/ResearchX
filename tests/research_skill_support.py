"""Test-only access to the independent bundled skill implementations."""

from importlib import import_module


SKILLS = {
    "financial": (
        "financial-statement-analysis",
        "analyze_statements",
        "calculate_financial",
        "FinancialResult",
    ),
    "monitor": ("company-event-monitor", "normalize_events", "normalize_monitor", "MonitorResult"),
    "digest": ("research-report-digest", "digest_reports", "normalize_digest", "DigestResult"),
    "deep": ("deep-investment-report", "forecast", "calculate_deep", "DeepResult"),
}


def module(kind, name):
    plugin = SKILLS[kind][0]
    return import_module(f"researchx.skills.bundled.{plugin}.scripts.{name}")


RESULT_TYPES = {kind: getattr(module(kind, "models"), config[3]) for kind, config in SKILLS.items()}
FUNCTIONS = {kind: getattr(module(kind, config[1]), config[2]) for kind, config in SKILLS.items()}
EXPORTERS = {kind: module(kind, "export_report").export_result for kind in SKILLS}


def export_result(result, *args, **kwargs):
    return EXPORTERS[result.kind](result, *args, **kwargs)


async def export_registered(result, directory, store, task_id=None):
    from researchx.contracts.models import Reference
    from researchx.state.models import SourceUsage
    from pydantic import BaseModel

    def references(value):
        if isinstance(value, Reference):
            yield value
        elif isinstance(value, BaseModel):
            for item in vars(value).values():
                yield from references(item)
        elif isinstance(value, dict):
            if "locator" in value and "title" in value:
                yield value
            for item in value.values():
                yield from references(item)
        elif isinstance(value, list):
            for item in value:
                yield from references(item)

    source_ids = {}
    evidence_sources = {}
    for ref in references(result):
        locator = ref["locator"] if isinstance(ref, dict) else ref.locator
        title = ref["title"] if isinstance(ref, dict) else ref.title
        evidence_id = ref.get("evidence_id") if isinstance(ref, dict) else ref.evidence_id
        existing_source = ref.get("source_id") if isinstance(ref, dict) else ref.source_id
        if existing_source:
            continue
        if locator not in source_ids:
            source = await store.capture(
                origin_id=f"test-fixture:{locator}", content="已核对的固定测试材料",
                kind="web", title=title, locator=locator,
                usage=SourceUsage(category="public", provider="test_fixture",
                                  licence_status="pending", model_use_allowed=True,
                                  internal_export_allowed=True, completeness="full"),
            )
            source_ids[locator] = source.id
        if isinstance(ref, dict):
            ref["source_id"] = source_ids[locator]
        else:
            ref.source_id = source_ids[locator]
        if evidence_id:
            evidence_sources[evidence_id] = source_ids[locator]
    if evidence_sources:
        from researchx.state.models import Evidence
        from researchx.storage.database import current_database

        async with current_database().transaction():
            memory = await store._load()
            for evidence_id, source_id in evidence_sources.items():
                source = memory.sources[source_id]
                memory.evidence_pool[evidence_id] = Evidence(
                    id=evidence_id, statement="已核对的固定测试材料",
                    source_id=source_id, collected_at=source.collected_at,
                    published_at=source.published_at,
                )
            await store._save(memory, "register_fixture_evidence", {})
    from researchx.workspace.exports import export_registered as registered
    exporter = module(result.kind, "export_report")
    return await registered(result, directory, store, task_id=task_id,
                            render_markdown=exporter.render_markdown, sheets=exporter.SHEETS)
