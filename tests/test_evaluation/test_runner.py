"""Real runtime with controlled providers; failures keep artifacts and closed spans."""

from types import SimpleNamespace

import pytest

from researchx.api.client import ApiMessageCompleteEvent
from researchx.api.usage import UsageSnapshot
from researchx.config import Settings
from researchx.engine.messages import ConversationMessage, TextBlock, ToolUseBlock
from researchx.evaluation.dataset import load_cases, DEFAULT_DATASET, dataset_version
from researchx.evaluation.report import read_results
from researchx.evaluation.runner import ExperimentRunner
from researchx.evaluation.models import Turn


@pytest.mark.asyncio
async def test_budget_override_grants_turns_without_changing_dataset_version(
    tmp_path, monkeypatch
):
    """长任务可用执行期预算覆盖拿到更多轮次，且不破坏归档数据的可重评分性。"""
    from researchx.evaluation import runner

    settings = Settings()
    settings.context_window_tokens = 200000
    monkeypatch.setattr(runner, "resolve_profile", lambda _: settings)
    monkeypatch.setattr(
        Settings, "resolve_auth", lambda _: SimpleNamespace(value="test-private-key")
    )
    cases = load_cases()
    frozen_version = dataset_version(cases)
    frozen_budget = cases[0].budget.model_calls
    experiment = ExperimentRunner(
        cases,
        DEFAULT_DATASET,
        tmp_path,
        profile="claude-api",
        client_factory=lambda _: Provider(),
        budget_overrides={"model_calls": frozen_budget + 20},
    )
    assert experiment.version == frozen_version
    artifact = await experiment.run_case(cases[0])
    assert artifact.dataset_version == frozen_version
    assert artifact.provenance["budget_overrides"] == {"model_calls": frozen_budget + 20}
    assert artifact.provenance["budget"]["model_calls"] == frozen_budget + 20
    # 冻结用例对象本身不被改写，避免影响同批其他执行与重评分。
    assert cases[0].budget.model_calls == frozen_budget


class Provider:
    def __init__(self, *, failure=False):
        self.closed, self.failure, self.requests = False, failure, []

    async def stream_message(self, request):
        self.requests.append(request)
        if self.failure:
            raise RuntimeError("upstream contains test-private-key")
        if len(self.requests) == 1:
            message = ConversationMessage(
                role="assistant",
                content=[
                    ToolUseBlock(
                        id="source",
                        name="read_file",
                        input={"path": "materials/financial-syn-01.json"},
                    )
                ],
            )
        else:
            message = ConversationMessage(
                role="assistant", content=[TextBlock(text="资料有限，需继续核验。")]
            )
        yield ApiMessageCompleteEvent(
            message=message,
            usage=UsageSnapshot(input_tokens=100, output_tokens=10, usage_reported=True),
        )

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_runtime_retains_records_and_never_passes_gold(tmp_path, monkeypatch, failure):
    from researchx.evaluation import runner

    settings = Settings()
    settings.context_window_tokens = 200000
    monkeypatch.setattr(runner, "resolve_profile", lambda _: settings)
    monkeypatch.setattr(
        Settings, "resolve_auth", lambda _: SimpleNamespace(value="test-private-key")
    )
    provider = Provider(failure=failure)
    experiment = ExperimentRunner(
        load_cases(),
        DEFAULT_DATASET,
        tmp_path,
        profile="claude-api",
        client_factory=lambda _: provider,
    )
    artifact = await experiment.run_case(load_cases()[0])
    assert provider.closed
    assert artifact.status == ("failed" if failure else "completed")
    assert all(o.status != "running" for o in artifact.observations)
    assert read_results(tmp_path)[0].trace_id == artifact.trace_id
    assert (tmp_path / "dataset" / "manifest.json").is_file()
    for request in provider.requests:
        text = str(request)
        assert "独立Decimal金标" not in text and "reference_facts" not in text
    assert "test-private-key" not in artifact.model_dump_json()
    if not failure:
        assert any(o.name == "read_file" and o.kind == "tool" for o in artifact.observations)


@pytest.mark.asyncio
async def test_runtime_startup_failure_closes_provider_and_retains_task(tmp_path, monkeypatch):
    from researchx.evaluation import runner

    settings = Settings(context_window_tokens=200000)
    monkeypatch.setattr(runner, "resolve_profile", lambda _: settings)
    monkeypatch.setattr(Settings, "resolve_auth", lambda _: SimpleNamespace(value="private-key"))

    async def failed_start(bundle):
        raise RuntimeError("startup failed")

    monkeypatch.setattr(runner, "start_runtime", failed_start)
    provider = Provider()
    experiment = ExperimentRunner(
        load_cases(),
        DEFAULT_DATASET,
        tmp_path,
        profile="claude-api",
        client_factory=lambda _: provider,
    )
    artifact = await experiment.run_case(load_cases()[0])
    assert provider.closed and artifact.status == "failed"
    assert read_results(tmp_path)[0].status == "failed"
    assert not provider.requests


@pytest.mark.asyncio
async def test_runtime_closes_provider_when_end_hook_fails(monkeypatch):
    from researchx.runtime import RuntimeBundle, close_runtime
    from researchx.sandbox import session

    async def no_sandbox():
        pass

    async def failed_hook(*args):
        raise RuntimeError("end hook failed")

    monkeypatch.setattr(session, "stop_docker_sandbox", no_sandbox)
    provider = Provider()
    bundle = RuntimeBundle(
        engine=SimpleNamespace(),
        tool_registry=None,
        session_started=True,
        cwd="/workspace",
        session_id="test-close",
        runtime_id="test-close-runtime",
        api_client=provider,
        mcp_manager=SimpleNamespace(close=no_sandbox),
        hook_executor=SimpleNamespace(execute=failed_hook),
    )
    with pytest.raises(RuntimeError):
        await close_runtime(bundle)
    assert provider.closed


@pytest.mark.asyncio
async def test_scripted_cancellation_and_resume_keeps_closed_turn_traces(tmp_path, monkeypatch):
    from researchx.evaluation import runner

    settings = Settings(context_window_tokens=200000)
    monkeypatch.setattr(runner, "resolve_profile", lambda _: settings)
    monkeypatch.setattr(Settings, "resolve_auth", lambda _: SimpleNamespace(value="private-key"))
    provider = Provider()
    cases = load_cases()
    case = cases[0].model_copy(deep=True)
    case.turns.append(
        Turn(prompt="恢复研究，保留资料限制。", action="cancel_resume", trigger="first_collection")
    )
    cases[0] = case
    experiment = ExperimentRunner(
        cases, DEFAULT_DATASET, tmp_path, profile="claude-api", client_factory=lambda _: provider
    )
    artifact = await experiment.run_case(case)
    turns = [o for o in artifact.observations if o.name == "user_turn"]
    assert [o.status for o in turns] == ["cancelled", "ok"]
    assert len(provider.requests) == 2 and provider.closed
    assert artifact.status == "completed" and all(
        o.status != "running" for o in artifact.observations
    )
