"""Execute cases using the real research runtime and persist every outcome."""

from __future__ import annotations

from typing import Callable, Protocol, Iterable
from researchx.config import Settings
from researchx.api.client import SupportsStreamingMessages
from researchx.evaluation.models import EvalCase, Turn, Budget
from researchx.runtime import RuntimeBundle
import asyncio
import shutil
import subprocess
import time
import hashlib
import json
import platform
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from uuid import uuid4
from researchx.config import load_settings
from researchx.engine.stream_events import (
    AssistantTextDelta,
    AssistantTurnComplete,
    ErrorEvent,
    ToolExecutionCompleted,
)
from researchx.evaluation.dataset import asset_path, directory_version
from researchx.evaluation.artifacts import read_deliverable
from researchx.evaluation.fixtures import configure_tools
from researchx.evaluation.judge import judge_with_retry, JudgeOutputError
from researchx.evaluation.models import RunArtifact
from researchx.evaluation.observer import RecordingObserver, fingerprint, timestamp
from researchx.evaluation.scoring import score_case
from researchx.state.store import ResearchStore
from researchx.runtime import (
    build_runtime,
    close_runtime,
    start_runtime,
    _resolve_api_client_from_settings,
)
from researchx.skills import load_skill_registry
from researchx.storage.filesystem import atomic_write_text


class ObserverFactory(Protocol):
    def __call__(self, *, secrets: Iterable[str], budget: Budget) -> RecordingObserver: ...


def write_artifact(path: str | Path, artifact: RunArtifact) -> None:
    atomic_write_text(Path(path), artifact.model_dump_json(indent=2) + "\n", mode=0o600)


def code_version() -> dict[str, object]:
    root = Path(__file__).resolve().parents[3]

    def git(*args: str) -> bytes:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, check=False).stdout

    untracked = git("ls-files", "--others", "--exclude-standard").decode().splitlines()
    content = git("diff", "HEAD")
    for name in sorted(untracked):
        p = root / name
        if p.is_file() and p.stat().st_size < 10_000_000:
            content += name.encode() + p.read_bytes()
    import hashlib

    dependencies = {}
    for package in ("researchx-ai", "langfuse", "pydantic", "httpx", "openai"):
        try:
            dependencies[package] = version(package)
        except PackageNotFoundError:
            pass
    return {
        "commit": git("rev-parse", "HEAD").decode().strip(),
        "working_tree_hash": hashlib.sha256(content).hexdigest(),
        "python_version": platform.python_version(),
        "dependencies": dependencies,
    }


def resolve_profile(name: str) -> Settings:
    settings = (
        load_settings()
        .model_copy(deep=True, update={"active_profile": name})
        .materialize_active_profile()
    )
    (settings.resolve_auth())  # Fail before any task starts if credentials are missing.
    return settings


class ExperimentRunner:
    def __init__(
        self,
        cases: list[EvalCase],
        directory: str | Path,
        output: str | Path,
        *,
        profile: str,
        judge_profile: str | None = None,
        observer_factory: ObserverFactory | None = None,
        client_factory: Callable[[Settings], SupportsStreamingMessages] | None = None,
        context_window_tokens: int | None = None,
        judge_context_window_tokens: int | None = None,
        budget_overrides: dict[str, int] | None = None,
    ) -> None:
        self.cases = {case.id: case for case in cases}
        self.directory, self.output = Path(directory), Path(output).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.profile, self.judge_profile = profile, judge_profile
        # 长任务预算覆盖：只在执行期生效，不参与 dataset_version 计算，
        # 因此不会让已归档结果失去可重评分性（见 _case_with_budget）。
        self.budget_overrides = dict(budget_overrides or {})
        self.agent_settings = resolve_profile(profile)
        self.judge_settings = resolve_profile(judge_profile) if judge_profile else None
        if self.judge_settings:
            self.judge_settings.timeout = max(120, self.judge_settings.timeout)
        if context_window_tokens:
            self.agent_settings.context_window_tokens = context_window_tokens
        if self.judge_settings and judge_context_window_tokens:
            self.judge_settings.context_window_tokens = judge_context_window_tokens
        if self.judge_settings:
            agent = self.agent_settings.resolve_profile()[1]
            judge = self.judge_settings.resolve_profile()[1]
            if (agent.provider, self.agent_settings.base_url, self.agent_settings.model) == (
                judge.provider,
                self.judge_settings.base_url,
                self.judge_settings.model,
            ):
                raise ValueError("被测模型与裁判必须使用不同模型配置")
        self.version = directory_version(self.directory, cases)
        # Archive inputs and gold locally so later corpus edits cannot change re-scoring.
        frozen = self.output / "dataset"
        if not frozen.exists():
            shutil.copytree(self.directory, frozen)
            atomic_write_text(
                frozen / "manifest.json",
                json.dumps(
                    {
                        "version": self.version,
                        "cases_sha256": hashlib.sha256(
                            (frozen / "cases.jsonl").read_bytes()
                        ).hexdigest(),
                    }
                ),
            )
        self.directory = frozen
        self.version_info = code_version()
        self.observer_factory = observer_factory or RecordingObserver
        self.client_factory = client_factory or _resolve_api_client_from_settings

    def _case_with_budget(self, case: EvalCase) -> EvalCase:
        """Apply execution-time budget overrides without touching dataset_version.

        model_calls is the turn ceiling for both the engine and the observer, so a
        genuinely long task can be granted more turns without regenerating the frozen
        dataset (which would change dataset_version and orphan archived results).
        """
        if not self.budget_overrides:
            return case
        budget = case.budget.model_copy(
            update={
                key: value
                for key, value in self.budget_overrides.items()
                if key in Budget.model_fields
            }
        )
        return case.model_copy(update={"budget": budget})

    async def run_case(
        self, case: EvalCase, *, repetition: int = 1, run_name: str | None = None
    ) -> RunArtifact:
        from researchx.storage.database import database_lifespan

        async with database_lifespan():
            return await self._run_case(case, repetition=repetition, run_name=run_name)

    async def _run_case(
        self, case: EvalCase, *, repetition: int = 1, run_name: str | None = None
    ) -> RunArtifact:
        case = self._case_with_budget(case)
        run_id = run_name or uuid4().hex
        workspace = self.output / "workspaces" / f"{case.id}-r{repetition}-{uuid4().hex[:8]}"
        workspace.mkdir(parents=True)
        (workspace / "materials").mkdir()
        for asset in case.assets:
            if asset.available_from_turn == 0:
                shutil.copyfile(
                    asset_path(self.directory, asset),
                    workspace / "materials" / Path(asset.path).name,
                )
        sid = uuid4().hex[:12]
        settings = self.agent_settings.model_copy(deep=True)
        settings.enabled_skills.update({name: False for name in case.disabled_skills})
        # Fixed replay never opens configured external MCP connections.
        if case.environment == "fixed":
            settings.mcp_servers = {}
        settings.permission.allowed_tools = list(
            dict.fromkeys(
                [
                    *settings.permission.allowed_tools,
                    "bash",
                    "write_file",
                    "edit_file",
                    "notebook_edit",
                ]
            )
        )
        settings.max_tokens = min(settings.max_tokens, 8192)
        secrets = [(settings.resolve_auth()).value]
        from researchx.security.redaction import memory_credentials, evaluation_credentials
        from researchx.api.tavily_search import tavily_credentials

        secrets.extend(memory_credentials() | tavily_credentials() | evaluation_credentials())
        if self.judge_settings:
            secrets.append((self.judge_settings.resolve_auth()).value)
        observer = self.observer_factory(secrets=secrets, budget=case.budget)
        artifact = RunArtifact(
            run_id=run_id,
            case_id=case.id,
            dataset_version=self.version,
            repetition=repetition,
            trace_id=uuid4().hex,
            started_at=timestamp(),
            provenance={
                **self.version_info,
                "profile": self.profile,
                "model": settings.model,
                "judge_profile": self.judge_profile,
                "environment": case.environment,
                "material": case.material,
                "category": case.category,
                "difficulty": case.difficulty,
                "split": case.split,
                "cache_condition": "unspecified",
                "model_parameters": {
                    "effort": settings.effort,
                    "max_tokens": settings.max_tokens,
                    "context_window_tokens": settings.context_window_tokens,
                    "timeout": settings.timeout,
                },
                # 便于区分“冻结预算”与“本次执行实际预算”。
                "budget": case.budget.model_dump(),
                "budget_overrides": dict(self.budget_overrides),
                "disabled_skills": case.disabled_skills,
                "permission_policy_hash": fingerprint(settings.permission.model_dump()),
            },
        )
        result_path = self.output / "results" / f"{case.id}-r{repetition}-{run_id}.json"
        start = time.monotonic()
        bundle: RuntimeBundle | None = None
        roots: list[Path] = []
        progress_counter = 0

        async def question(text: str) -> str:
            return case.turns[min(progress_counter, len(case.turns) - 1)].question_response

        async def permission(tool: str, reason: str) -> bool:
            # Scripted policy is fixed per experiment; it does not depend on user response latency.
            return tool in {
                "bash",
                "write_file",
                "edit_file",
                "notebook_edit",
                "mcp__fixture__get_company_data",
            }

        async def make_bundle(restore: list[dict[str, object]] | None = None) -> RuntimeBundle:
            client = self.client_factory(settings)
            current = None
            try:
                current = await build_runtime(
                    cwd=str(workspace),
                    active_profile=self.profile,
                    api_client=client,
                    session_id=sid,
                    settings_override=settings,
                    research_store_override=ResearchStore(
                        workspace, sid, root=workspace / "session-state"
                    ),
                    observer=observer,
                    connect_mcp=case.environment == "live",
                    max_turns=case.budget.model_calls,
                    context_window_tokens=settings.context_window_tokens,
                    ask_user_prompt=question,
                    permission_prompt=permission,
                    restore_messages=restore,
                )
                current.engine.tool_metadata["research_store"] = ResearchStore(
                    workspace, sid, root=workspace / "session-state"
                )
                current.engine.tool_metadata["skill_settings"] = settings
                if case.environment == "fixed":
                    from researchx.hooks.loader import HookRegistry

                    current.hook_executor.update_registry(HookRegistry())
                    artifact.provenance["external_hooks"] = "disabled_fixed_environment"
                skills = (load_skill_registry(workspace, settings=settings)).list_skills()
                # An evaluation explicitly freezes resource provenance once per
                # case; this verification is separate from L0 registry discovery.
                from researchx.skills.metadata import content_hash

                roots[:] = [Path(skill.base_dir) for skill in skills if skill.base_dir]
                (configure_tools(current, case, workspace, roots))
                artifact.provenance.update(
                    {
                        "prompt_hash": fingerprint(current.engine.system_prompt),
                        "tools_hash": fingerprint(current.tool_registry.to_api_schema()),
                        "skills": [
                            {
                                "id": s.metadata.skill_id or s.name,
                                "version": s.metadata.version,
                                "content_hash": content_hash(Path(s.path))
                                if s.path
                                else s.metadata.content_hash,
                            }
                            for s in skills
                        ],
                    }
                )
                await start_runtime(current)
                return current
            except BaseException as exc:
                if current is not None:
                    try:
                        await close_runtime(current)
                    except BaseException as cleanup_error:
                        setattr(exc, "cleanup_errors", [cleanup_error])
                raise

        async def execute_turn(
            turn: Turn, *, interrupt: bool = False, interrupt_trigger: str = "first_tool"
        ) -> None:
            assert bundle is not None
            input_prompt = turn.prompt
            if progress_counter == 0:
                files = "\n".join(
                    f"- materials/{Path(a.path).name}：{a.title}；原始定位 {a.locator}"
                    for a in case.assets
                    if a.available_from_turn == 0
                )
                input_prompt += f"\n资料截止：{case.cutoff}\n本轮资料：\n{files}"
                if case.environment == "fixed":
                    input_prompt += (
                        "\n这是固定材料评测，合成资料不对应现实证券身份。仅使用提供材料或冻结工具响应。"
                        "未知项说明缺口，不扩大联网。bash 仅支持单次 Python 调用或已启用技能脚本。"
                        "所有输出文件保存到工作区或当前会话目录；需引用本会话来源。"
                    )
                else:
                    input_prompt += (
                        "\nbash 支持单次 Python 调用和已启用技能脚本；联网使用网络工具"
                        "或 Python HTTP 客户端，文件读写限本轮工作区。\n官方/机构资料线索：\n"
                        + "\n".join(case.live_urls)
                    )
            with observer.span(
                "user_turn", input={"text": input_prompt, "action": turn.action}
            ) as turn_span:
                generator = bundle.engine.submit_message(input_prompt)
                async for event in generator:
                    if (
                        isinstance(event, AssistantTextDelta)
                        and event.text
                        and artifact.first_content_ms is None
                    ):
                        artifact.first_content_ms = (time.monotonic() - start) * 1000
                    elif isinstance(event, AssistantTurnComplete) and not event.message.tool_uses:
                        artifact.answer = observer.clean(event.message.text)
                        if artifact.answer and artifact.first_content_ms is None:
                            artifact.first_content_ms = (time.monotonic() - start) * 1000
                    elif isinstance(event, ErrorEvent):
                        artifact.error = observer.clean(event.message)
                        artifact.status = "failed"
                    if interrupt and isinstance(event, ToolExecutionCompleted):
                        if interrupt_trigger == "first_collection" and event.tool_name not in {
                            "read_file",
                            "web_fetch",
                            "web_search",
                        }:
                            continue
                        await generator.aclose()
                        (await bundle.engine.tool_metadata["research_store"].stopped())
                        turn_span.update(
                            status="cancelled",
                            metadata={"scripted_interruption": True, "trigger": interrupt_trigger},
                        )
                        return

        try:
            with observer.span(
                "agent_task",
                kind="agent",
                input=case.agent_input(),
                metadata={"case_id": case.id, "dataset_version": self.version},
            ) as task_observation:
                bundle = await asyncio.wait_for(make_bundle(), timeout=case.budget.timeout_seconds)
                task_observation.update(
                    metadata={
                        **artifact.provenance,
                        "case_id": case.id,
                        "dataset_version": self.version,
                    }
                )

                async def workflow() -> None:
                    nonlocal bundle, progress_counter
                    assert bundle is not None, progress_counter
                    for index, turn in enumerate(case.turns):
                        progress_counter = index
                        for asset in case.assets:
                            if asset.available_from_turn == index and index:
                                shutil.copyfile(
                                    asset_path(self.directory, asset),
                                    workspace / "materials" / Path(asset.path).name,
                                )
                                if asset.replaces_asset_id:
                                    original = next(
                                        a for a in case.assets if a.id == asset.replaces_asset_id
                                    )
                                    shutil.copyfile(
                                        asset_path(self.directory, asset),
                                        workspace / "materials" / Path(original.path).name,
                                    )
                        # A following script turn can interrupt the current one at a real tool boundary.
                        following = case.turns[index + 1] if index + 1 < len(case.turns) else None
                        interrupt = following is not None and following.action in {
                            "steer",
                            "cancel_resume",
                        }
                        if turn.action == "restart":
                            restore = [m.model_dump(mode="json") for m in bundle.engine.messages]
                            await close_runtime(bundle)
                            bundle = await make_bundle(restore)
                        if turn.action == "steer":
                            store = bundle.engine.tool_metadata["research_store"]
                            request_id = uuid4().hex
                            (
                                await store.interrupt(
                                    request_id=request_id,
                                    target_request_id="evaluation",
                                    text=turn.prompt,
                                )
                            )
                            (await store.require_replan(request_id))
                        await execute_turn(
                            turn,
                            interrupt=interrupt,
                            interrupt_trigger=following.trigger if following else "first_tool",
                        )

                remaining = case.budget.timeout_seconds - (time.monotonic() - start)
                await asyncio.wait_for(workflow(), timeout=max(0, remaining))
                if time.monotonic() - start > case.budget.timeout_seconds:
                    raise asyncio.TimeoutError()
                if artifact.status == "running":
                    artifact.status = "completed" if artifact.answer else "failed"
                task_observation.update(
                    status="ok" if artifact.status == "completed" else "error",
                    output={
                        "answer": artifact.answer,
                        "status": artifact.status,
                        "error": artifact.error,
                    },
                )
        except asyncio.TimeoutError:
            artifact.status, artifact.error = "timeout", "任务达到墙钟预算"
        except asyncio.CancelledError:
            artifact.status, artifact.error = "cancelled", "评测执行已取消"
            raise
        except Exception as exc:
            artifact.status, artifact.error = "failed", f"执行失败：{type(exc).__name__}"
        finally:
            artifact.elapsed_ms = (time.monotonic() - start) * 1000
            end_version = code_version()
            artifact.provenance["ending_working_tree_hash"] = end_version["working_tree_hash"]
            artifact.provenance["code_changed_during_experiment"] = (
                end_version["working_tree_hash"] != self.version_info["working_tree_hash"]
            )
            artifact.observations = observer.observations
            artifact.trace_blobs = observer.blobs
            first_usage = next(
                (o.usage for o in observer.observations if o.kind == "generation"), None
            )
            if first_usage and first_usage.get("cache_read_input_tokens") is not None:
                artifact.provenance["cache_condition"] = (
                    "first_call_cache_hit"
                    if first_usage["cache_read_input_tokens"]
                    else "first_call_cache_miss"
                )
            if getattr(observer, "trace_id", None):
                artifact.trace_id = observer.trace_id or artifact.trace_id
                artifact.upload_status = "failed" if observer.export_failed else "pending"
            if bundle:
                try:
                    (await self.capture_result(bundle, workspace, artifact, observer))
                except Exception as exc:
                    artifact.provenance["capture_error"] = type(exc).__name__
                    artifact.status = "failed"
                    artifact.error = artifact.error or "研究状态或产物读取失败"
                try:
                    await close_runtime(bundle)
                except Exception as exc:
                    artifact.provenance["cleanup_error"] = type(exc).__name__
            artifact.scores = score_case(case, artifact)
            write_artifact(result_path, artifact)
        if self.judge_settings:
            judge_client = None
            try:
                judge_client = self.client_factory(self.judge_settings)
                judgment = await asyncio.wait_for(
                    (
                        judge_with_retry(
                            case,
                            artifact,
                            judge_client,
                            self.judge_settings.model,
                            context_window_tokens=self.judge_settings.context_window_tokens,
                        )
                    ),
                    # 覆盖整个重试循环：大报文裁判单次可跑数分钟，限流退避还需要额外余量。
                    timeout=900,
                )
                artifact.judge_results = judgment.model_dump()
                artifact.scores = score_case(case, artifact, judgment)
            except Exception as exc:
                reason = str(exc) if isinstance(exc, JudgeOutputError) else type(exc).__name__
                artifact.provenance["judge_error"] = f"裁判结果未判定：{reason}"
                artifact.scores = score_case(case, artifact)
            finally:
                close = getattr(judge_client, "close", None)
                if close:
                    try:
                        await close()
                    except Exception:
                        artifact.provenance["judge_close_error"] = "裁判连接关闭失败"
        else:
            artifact.scores = score_case(case, artifact)
        artifact = RunArtifact.model_validate(observer.clean(artifact.model_dump()))
        write_artifact(result_path, artifact)
        return artifact

    @staticmethod
    async def capture_result(
        bundle: RuntimeBundle, workspace: Path, artifact: RunArtifact, observer: RecordingObserver
    ) -> None:
        store = bundle.engine.tool_metadata["research_store"]
        state = await store.load()
        artifact.research_state = observer.clean(state.model_dump(mode="json"))
        for key, source in state.sources.items():
            try:
                artifact.sources[key] = observer.clean((await store.read_source(source)))
            except (ValueError, OSError):
                artifact.provenance.setdefault("missing_sources", []).append(key)
        from researchx.workspace.session_files import SessionFiles

        files = SessionFiles(store)
        paths = {}
        for path in workspace.rglob("*"):
            if (
                path.is_file()
                and path.suffix in {".md", ".json", ".docx", ".xlsx"}
                and not path.is_relative_to(workspace / "materials")
                and not path.is_relative_to(workspace / "session-state")
                and not path.is_relative_to(workspace / ".researchx")
            ):
                paths[str(path.relative_to(workspace))] = (path, False)
        for item in await files.list("artifacts"):
            try:
                _, path = await files.artifact(item["id"])
                paths[item["name"]] = (path, True)
            except (ValueError, OSError, KeyError):
                artifact.provenance.setdefault("invalid_artifacts", []).append(item["id"])
        for name, (path, registered) in paths.items():
            try:
                text, metadata = read_deliverable(path)
                artifact.artifacts[name] = observer.clean(text)
                artifact.artifact_metadata[name] = {**metadata, "registered": registered}
            except Exception as exc:
                artifact.artifact_metadata[name] = {"valid": False, "error": type(exc).__name__}
