"""Report lifecycle invariants, optimistic concurrency and version isolation."""

import asyncio

import pytest
from tests.postgres_helpers import source_path

from researchx.state.completion import CompletionPolicy, ResearchContext
from researchx.state.errors import ResearchError
from researchx.state.models import (
    PlanPatch,
    PlanProposal,
    ResearchObjective,
    ResearchTask,
    TaskRevision,
    new_id,
)
from researchx.state.repository import ResearchRepository
from researchx.state.runtime import ResearchAgentRuntime
from researchx.state.store import ResearchStore
from researchx.state.tasks import validate_dag
from researchx.workspace.session_files import SessionFiles
from tests.test_research.test_store import apply, reasoning


def task(key="sources", *, dependencies=None, kind="note", criterion=None):
    return ResearchTask(
        id=key,
        title=key,
        dependencies=dependencies or [],
        acceptance_criteria=[criterion or key],
        required_artifact_kinds=[kind],
    )


@pytest.fixture
async def project(tmp_path):
    store = ResearchStore(tmp_path, "c" * 12, root=tmp_path / "memory")
    user = (await store.capture(origin_id="user", kind="user", content="生成公司财报点评"))
    repo = ResearchRepository(store)
    (await repo.start(
        ResearchObjective(
            project_id="project_A",
            subject="公司 A",
            report_type="earnings_commentary",
            requirements=["sources", "draft"],
            deliverables=["report_draft"],
            required_sections=["摘要"],
        ),
        [user.id],
        (await store.load()).revision,
    ))
    return repo


async def commit(repo, tasks=None):
    tasks = tasks or [task(), task("draft", dependencies=["sources"], kind="report_draft")]
    proposal = PlanProposal(objective_revision=1, tasks=tasks, rationale="来源先于初稿")
    return (await repo.commit_plan("project_A", proposal, (await repo.store.load()).revision))


async def claim(repo, key):
    memory = (await repo.store.load())
    selected = repo._plan(memory)
    item = next(item for item in selected.tasks if item.id == key)
    return (await repo.transition_task(
        key, "ready", "in_progress", memory.revision, task_revision=item.task_revision
    ))


async def checked_evidence(repo, key="sources"):
    execution = (await repo.begin_execution("fixture_source", new_id("tool")))
    receipt = (await repo.commit_execution(
        execution["id"],
        [{"content": "营收120百万元人民币，FY2025", "kind": "file", "locator": "fixture:earnings"}],
    ))
    from researchx.state.deliveries import DeliveryService, TrustedReviewer
    from researchx.state.models import SourceUsage

    await DeliveryService(repo.store).classify_source(
        receipt["research_sources"][0],
        SourceUsage(category="public", provider="offline_public_fixture", licence_status="pending",
                    model_use_allowed=True, internal_export_allowed=True, completeness="full"),
        principal=TrustedReviewer("fixture-curator", "compliance", "trusted_identity_provider"),
    )
    ev = (await apply(
        repo.store,
        "add_evidence",
        statement="营收120百万元人民币，FY2025",
        source_id=receipt["research_sources"][0],
    ))["evidence_id"]
    step = (await reasoning(repo.store, [ev], verification=True))
    checked = (await apply(
        repo.store,
        "verify_evidence",
        evidence_id=ev,
        level="source_checked",
        method="source",
        verification_step_id=step,
        verification_note="核对离线完整披露 fixture",
    ))["evidence_id"]
    return execution, checked


async def artifact(repo, execution, ev, *, key="sources", kind="note", **fields):
    data = {
        "task_id": key,
        "task_revision": execution["task_revision"],
        "plan_revision": execution["plan_revision"],
        "execution_id": execution["id"],
        "kind": kind,
        "title": key,
        "content": "核对完整披露",
        "criteria": [key],
        "evidence_ids": [ev],
        **fields,
    }
    return (await repo.submit_artifact(data, (await repo.store.load()).revision, new_id("op")))["artifact_id"]


async def finish_sources(repo):
    (await claim(repo, "sources"))
    execution, ev = (await checked_evidence(repo))
    art = (await artifact(repo, execution, ev))
    result = (await repo.request_task_completion("project_A", "sources", (await repo.store.load()).revision, 1))
    assert result["passed"]
    return ev, art


async def test_initial_plan_dag_and_legacy_file_loading(project):
    repo = project
    (await commit(repo))
    memory = (await repo.store.load())
    plan = repo._plan(memory)
    assert [item.status for item in plan.tasks] == ["ready", "pending"]
    assert plan.tasks[1].dependency_revisions == {"sources": 1}
    restored = ResearchStore(
        repo.store.cwd, repo.store.session_id, root=repo.store.directory.parent
    )
    assert (await restored.load()).project.id == "project_A"
    with pytest.raises(ResearchError, match="transition|dependencies"):
        (await repo.transition_task("draft", "pending", "in_progress", memory.revision, task_revision=1))
    with pytest.raises(ResearchError, match="bypass"):
        (await apply(repo.store, "create_plan", title="绕过", tasks=["任意完成"]))


@pytest.mark.parametrize(
    "tasks,match",
    [
        ([task("a", dependencies=["b"]), task("b", dependencies=["a"])], "cycle"),
        ([task("a", dependencies=["missing"])], "Missing"),
        ([task("a"), task("a")], "Duplicate"),
    ],
)
async def test_bad_dags_rejected_without_commit(project, tasks, match):
    before = (await project.store.load()).revision
    with pytest.raises(ResearchError, match=match):
        (await commit(project, tasks))
    assert (await project.store.load()).revision == before
    validate_dag([task("a"), task("b", dependencies=["a"])], proposal=True)


async def test_claim_is_exclusive_and_repeated_transition_rejected(project):
    (await commit(project, [task(), task("independent")]))
    (await claim(project, "sources"))
    with pytest.raises(ResearchError, match="active task"):
        (await claim(project, "independent"))
    with pytest.raises(ResearchError, match="active task"):
        (await claim(project, "sources"))


async def test_evidence_missing_blocks_completion_and_project(project):
    (await commit(project))
    (await claim(project, "sources"))
    execution = (await project.begin_execution("fixture", "missing-evidence"))
    (await project.commit_execution(execution["id"], []))
    (await artifact(project, execution, ev="unused", evidence_ids=[]))
    result = (await project.request_task_completion(
        "project_A", "sources", (await project.store.load()).revision, 1
    ))
    assert not result["passed"]
    assert any("checked evidence" in value for value in result["missing_requirements"])
    assert project._plan((await project.store.load())).tasks[0].status == "in_progress"
    final = (await project.finalize("project_A", (await project.store.load()).revision))
    assert not final["passed"] and (await project.store.load()).project.status == "running"


async def test_concurrent_plan_cas_only_one_writer(project):
    proposal = PlanProposal(objective_revision=1, tasks=[task()], rationale="来源")
    revision = (await project.store.load()).revision

    async def worker(_):
        try:
            (await project.commit_plan("project_A", proposal, revision))
            return True
        except ResearchError as exc:
            assert "conflict" in str(exc)
            return False

    assert sum(await asyncio.gather(*(worker(index) for index in range(2)))) == 1
    assert len((await project.store.load()).plans) == 1


async def test_patch_preserves_completed_facts_and_invalidates_descendants(project):
    (await commit(project))
    ev, source_artifact = (await finish_sources(project))
    (await claim(project, "draft"))
    execution, draft_ev = (await checked_evidence(project, "draft"))
    draft = (await artifact(
        project,
        execution,
        draft_ev,
        key="draft",
        kind="report_draft",
        content=f"# 摘要\n营收增长 [E:{draft_ev}]",
        sections=["摘要"],
        input_artifact_ids=[source_artifact],
    ))
    assert (await project.request_task_completion("project_A", "draft", (await project.store.load()).revision, 1))[
        "passed"
    ]
    old = project._plan((await project.store.load()))
    (await project.submit_feedback("project_A", "增加储能增长预测", (await project.store.load()).revision))
    patch = PlanPatch(
        base_plan_revision=1,
        objective_revision=2,
        add_tasks=[task("storage", criterion="增加储能增长预测")],
        revise_tasks=[
            TaskRevision(
                task_id="draft",
                expected_revision=1,
                replacement=task("draft", dependencies=["sources", "storage"], kind="report_draft"),
            )
        ],
        reason="新增储能研究，修订初稿",
    )
    (await project.apply_plan_patch("project_A", patch, (await project.store.load()).revision))
    memory = (await project.store.load())
    current = project._plan(memory)
    assert current.tasks[0].status == "completed" and current.tasks[0].task_revision == 1
    assert current.tasks[1].task_revision == 2 and current.tasks[1].artifact_ids == []
    assert current.tasks[1].dependency_revisions == {"sources": 1, "storage": 1}
    assert memory.plans[old.id].tasks[1].status == "completed"
    assert memory.artifacts[draft].stale and not memory.artifacts[source_artifact].stale
    assert not memory.evidence_pool[ev].needs_review
    with pytest.raises(ResearchError, match="Plan revision conflict"):
        (await project.apply_plan_patch("project_A", patch, memory.revision))


async def test_upstream_revision_propagates_to_downstream(project):
    (await commit(project))
    _, source_artifact = (await finish_sources(project))
    patch = PlanPatch(
        base_plan_revision=1,
        objective_revision=1,
        reason="来源口径改变",
        revise_tasks=[TaskRevision(task_id="sources", expected_revision=1, replacement=task())],
    )
    (await project.apply_plan_patch("project_A", patch, (await project.store.load()).revision))
    memory = (await project.store.load())
    assert [item.task_revision for item in project._plan(memory).tasks] == [2, 2]
    assert memory.artifacts[source_artifact].stale
    assert memory.plans[next(iter(memory.plans))].tasks[0].status == "completed"


async def test_empty_plan_after_patch_rejected(project):
    (await commit(project))
    patch = PlanPatch(
        base_plan_revision=1,
        objective_revision=1,
        reason="取消所有工作",
        cancel_task_ids=["sources", "draft"],
    )
    with pytest.raises(ResearchError, match="No executable"):
        (await project.apply_plan_patch("project_A", patch, (await project.store.load()).revision))
    assert (await project.store.load()).project.plan_revision == 1


async def test_interrupt_preserves_plan_resumes_and_discards_late_results(project):
    (await commit(project))
    (await claim(project, "sources"))
    execution = (await project.begin_execution("slow_fetch", "old-call"))
    old_plan = project._plan((await project.store.load())).id
    (await project.store.interrupt(request_id="steer", target_request_id="old", text="增加储能"))
    (await project.store.stopped())
    (await project.store.require_replan("steer"))
    memory = (await project.store.load())
    assert memory.research_state.current_plan_id == old_plan
    assert memory.project.objective_revision == 2 and memory.research_state.replan_required
    late = (await project.commit_execution(execution["id"], [{"content": "旧执行返回的预测"}]))
    assert not late["accepted"] and not late["research_sources"]
    assert not any(item.origin_id == "old-call" for item in (await project.store.load()).sources.values())
    (await project.store.require_replan("steer"))  # Idempotent recovery boundary.
    assert (await project.store.load()).project.objective_revision == 2
    patch = PlanPatch(
        base_plan_revision=1,
        objective_revision=2,
        reason="新增研究",
        add_tasks=[task("storage", criterion="增加储能")],
    )
    (await project.apply_plan_patch("project_A", patch, (await project.store.load()).revision))
    (await project.suspend("Execution interrupted"))
    (await project.resume("project_A"))
    assert (await project.store.load()).project.status == "running"


async def test_restart_revokes_unfinished_execution_without_rerun(project):
    (await commit(project))
    (await claim(project, "sources"))
    execution = (await project.begin_execution("fixture", "before-crash"))
    restored = ResearchRepository(project.store)
    (await restored.recover())
    assert (await restored.store.load()).project.status == "suspended"
    assert (await restored.store.load()).executions[execution["id"]].status == "cancelled"
    (await restored.resume("project_A"))
    assert restored._plan((await restored.store.load())).tasks[0].status == "ready"


async def test_runtime_api_and_early_stop(project):
    runtime = ResearchAgentRuntime(project.store)
    assert not (await runtime.evaluate_stop()).passed
    (await commit(project))
    assert not (await runtime.finalize("project_A")).passed
    await runtime.interrupt("project_A", "Execution interrupted")
    await runtime.resume("project_A")
    await runtime.submit_feedback("project_A", "增加储能")
    assert (await project.store.load()).project.status == "replanning"


@pytest.mark.parametrize(
    "fields,check",
    [
        ({"unit": "", "currency": "CNY", "period": "FY2025"}, "dimensions"),
        (
            {"unit": "million", "currency": "CNY", "period": "FY2025", "reproduction": ""},
            "reproducible",
        ),
    ],
)
async def test_financial_artifact_quality_checks(project, fields, check):
    (await commit(project, [task(kind="model")]))
    (await claim(project, "sources"))
    execution, ev = (await checked_evidence(project))
    identifier = (await artifact(project, execution, ev, kind="model", **fields))
    memory = (await project.store.load())
    checks = (await CompletionPolicy().artifact_checks(
        memory.artifacts[identifier], ResearchContext(memory, project.store)
    ))
    assert any(item.check_id.endswith(check) and not item.passed for item in checks)


async def test_draft_snapshot_and_stale_citations_block_delivery(project):
    (await commit(project))
    _, source_artifact = (await finish_sources(project))
    (await claim(project, "draft"))
    execution, ev = (await checked_evidence(project, "draft"))
    identifier = (await artifact(
        project,
        execution,
        ev,
        key="draft",
        kind="report_draft",
        content="# 摘要\n没有可核验引用 [E:unknown]",
        sections=["摘要"],
        input_artifact_ids=[source_artifact],
    ))
    result = (await project.request_task_completion("project_A", "draft", (await project.store.load()).revision, 1))
    assert not result["passed"]
    memory = (await project.store.load())
    (await source_path(project.store, memory.artifacts[identifier])).write_text("tampered")
    checks = (await CompletionPolicy().artifact_checks(
        memory.artifacts[identifier], ResearchContext(memory, project.store)
    ))
    assert any(item.check_id.endswith("snapshot") and not item.passed for item in checks)


async def test_artifact_submission_is_idempotent_and_rejects_old_lease(project):
    (await commit(project))
    (await claim(project, "sources"))
    execution, ev = (await checked_evidence(project))
    data = {
        "task_id": "sources",
        "task_revision": 1,
        "plan_revision": 1,
        "execution_id": execution["id"],
        "kind": "note",
        "title": "note",
        "content": "原文已核对",
        "criteria": ["sources"],
        "evidence_ids": [ev],
    }
    rev = (await project.store.load()).revision
    first = (await project.submit_artifact(data, rev, "artifact-op"))
    assert (await project.submit_artifact(data, rev, "artifact-op")) == first
    (await project.suspend("Execution interrupted"))
    (await project.resume("project_A"))
    (await claim(project, "sources"))
    with pytest.raises(ResearchError, match="active lease"):
        (await project.submit_artifact(data, (await project.store.load()).revision, "new-artifact-op"))


async def test_project_cancel_preserves_committed_history(project):
    (await commit(project))
    _, identifier = (await finish_sources(project))
    (await project.cancel("project_A", "用户终止", (await project.store.load()).revision))
    memory = (await project.store.load())
    assert memory.project.status == "cancelled" and identifier in memory.artifacts
    assert project._plan(memory).tasks[0].status == "completed"


async def test_two_execution_results_can_commit_under_one_task_lease(project):
    (await commit(project))
    (await claim(project, "sources"))
    first = (await project.begin_execution("read_file", "one"))
    second = (await project.begin_execution("read_file", "two"))
    receipts = await asyncio.gather(
        *[
            project.commit_execution(execution["id"], [{"content": execution["id"]}])
            for execution in (first, second)
        ]
    )
    assert all(item["accepted"] for item in receipts)
    assert first["lease_id"] == second["lease_id"]


async def test_export_manifest_retains_execution_versions_and_projects_stale(project, tmp_path):
    (await commit(project))
    (await claim(project, "sources"))
    execution = (await project.begin_execution("bash", "export"))
    path = tmp_path / "draft.md"
    path.write_text("离线草稿")
    files = SessionFiles(project.store)
    manifest = (await files.register(
        path, task_id="sources", status="draft", kind="note", execution=execution
    ))
    assert (await files.list("artifacts"))[0]["status"] == "pending_execution"
    (await project.commit_execution(execution["id"], []))
    assert not (await files.artifact(manifest["id"]))[0]["stale"]
    (await project.apply_plan_patch(
        "project_A",
        PlanPatch(
            base_plan_revision=1,
            objective_revision=1,
            reason="修订来源任务",
            revise_tasks=[TaskRevision(task_id="sources", expected_revision=1, replacement=task())],
        ),
        (await project.store.load()).revision,
    ))
    metadata, retained_path = (await files.artifact(manifest["id"]))
    assert (
        metadata["stale"]
        and metadata["task_revision"] == 1
        and metadata["execution_id"] == execution["id"]
    )
    assert retained_path.read_text() == "离线草稿"


async def test_evidence_correction_marks_artifacts_stale_and_requires_replan(project):
    (await commit(project))
    ev, identifier = (await finish_sources(project))
    (await claim(project, "draft"))
    source = (await project.store.load()).evidence_pool[ev].source_id
    (await apply(project.store, "add_evidence", source_id=source, statement="纠正数据口径", supersedes=ev))
    memory = (await project.store.load())
    assert memory.artifacts[identifier].stale and memory.research_state.replan_required
    assert memory.project.status == "replanning"
    (await project.apply_plan_patch(
        "project_A",
        PlanPatch(base_plan_revision=1, objective_revision=1, reason="重做更正后的来源与初稿"),
        memory.revision,
    ))
    assert [item.task_revision for item in project._plan((await project.store.load())).tasks] == [2, 2]


async def test_active_execution_must_settle_before_task_validation(project):
    (await commit(project))
    (await claim(project, "sources"))
    execution = (await project.begin_execution("slow_tool", "active"))
    with pytest.raises(ResearchError, match="settle"):
        (await project.request_task_completion("project_A", "sources", (await project.store.load()).revision, 1))
    assert (await project.store.load()).executions[execution["id"]].status == "running"


async def test_planning_budget_exhaustion_requires_explicit_host_budget_change(project):
    baseline = (await project.reserve_planning("planner"))
    (await project.set_planning_budget(
        "project_A", max_calls=2, max_tokens=64000, expected_revision=baseline["revision"]
    ))
    (await project.reserve_planning("planner"))
    with pytest.raises(ResearchError, match="budget exhausted"):
        (await project.reserve_planning("planner"))
    assert (await project.store.load()).project.status == "suspended"
    with pytest.raises(ResearchError, match="budget exhausted"):
        (await project.resume("project_A"))
    (await project.set_planning_budget(
        "project_A", max_calls=3, max_tokens=64000, expected_revision=(await project.store.load()).revision
    ))
    (await project.resume("project_A"))
    assert (await project.store.load()).project.status == "planning"


async def test_repeated_execution_commit_is_idempotent_and_conflicting_replay_rejected(project):
    (await commit(project))
    (await claim(project, "sources"))
    execution = (await project.begin_execution("fixture", "replay"))
    specs = [{"content": "实际资料"}]
    first = (await project.commit_execution(execution["id"], specs))
    second = (await project.commit_execution(execution["id"], specs))
    assert first["research_sources"] == second["research_sources"]
    assert len((await project.store.load()).sources) == 2  # User objective plus one tool source.
    with pytest.raises(ResearchError, match="different content"):
        (await project.commit_execution(execution["id"], [{"content": "不同资料"}]))


async def test_tampered_source_snapshot_blocks_task_completion(project):
    (await commit(project))
    (await claim(project, "sources"))
    execution, ev = (await checked_evidence(project))
    (await artifact(project, execution, ev))
    memory = (await project.store.load())
    source = memory.sources[memory.evidence_pool[ev].source_id]
    (await source_path(project.store, source)).write_text("原文内容被修改")
    result = (await project.request_task_completion("project_A", "sources", memory.revision, 1))
    assert not result["passed"]
    assert any("source snapshots" in message for message in result["missing_requirements"])
    assert project._plan((await project.store.load())).tasks[0].status == "in_progress"
