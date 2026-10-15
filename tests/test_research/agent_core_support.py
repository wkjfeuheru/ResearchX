"""Offline protocol drivers; project control, files, evidence and policy are real."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from researchx.api.client import ApiMessageCompleteEvent
from researchx.api.usage import UsageSnapshot
from researchx.config.settings import Settings
from researchx.engine.messages import (
    ConversationMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from researchx.engine.stream_events import ErrorEvent, ToolExecutionCompleted
from researchx.state.models import PlanProposal, ResearchObjective, ResearchTask, new_id
from researchx.state.store import ResearchStore
from researchx.runtime import build_runtime, close_runtime
from researchx.tools.base import BaseTool, ToolResult
from researchx.tools.web_fetch_tool import WebFetchToolInput

REQUEST = (
    "请对虚构上市公司 AlphaTech 初步研究：公司基本情况、行业竞争格局和近期财务表现，生成研究摘要。"
)
FEEDBACK = "增加海外业务分析，并将海外收入增长作为重点。"
DOCS = {
    "company": "合成资料：AlphaTech 成立于2015年，主营工业传感器；资料仅用于测试。",
    "industry": "合成资料：AlphaTech 与 BetaSensors、GammaDevices 竞争，差异在可靠性。",
    "financial": "合成公告：FY2025收入120百万元人民币，FY2024收入100，净利润12。",
    "overseas": "合成公告：FY2025海外收入30百万元人民币，FY2024海外收入20。",
}
USAGE = UsageSnapshot(input_tokens=7, output_tokens=3)


def last_result(request):
    return next(
        (
            block
            for message in reversed(request.messages)
            for block in reversed(message.content)
            if isinstance(block, ToolResultBlock)
        ),
        None,
    )


def json_result(block):
    return json.JSONDecoder().raw_decode(block.content)[0]


def workspace_packet(request):
    text = next(
        message.runtime_context
        for message in reversed(request.messages)
        if message.runtime_context and "<research_memory>" in message.runtime_context
    )
    body = text.split("<research_memory>", 1)[1].split("</research_memory>", 1)[0]
    return json.loads(body[body.index('{"') :])


class OfflineFetch(BaseTool):
    """Replace only network I/O under the existing web_fetch contract."""

    name = "web_fetch"
    description = "Retrieve the fixed AlphaTech disclosure fixture without network access"
    input_model = WebFetchToolInput

    def __init__(self):
        self.calls = []

    def is_read_only(self, arguments):
        return True

    async def execute(self, arguments, context):
        key = arguments.url.rsplit("/", 1)[-1]
        self.calls.append((key, context.cwd, bool(context.metadata.get("subagent_child"))))
        return ToolResult(
            output=DOCS[key],
            metadata={
                "research_source_specs": [
                    {
                        "kind": "web",
                        "content": DOCS[key],
                        "locator": "https://www.sse.com.cn/test-fixtures/" + key,
                        "title": f"AlphaTech {key} 合成披露",
                        "fragment": False,
                    }
                ]
            },
        )


class ScriptModel:
    """One real main loop executing explicitly supplied model tool calls."""

    def __init__(self, steps):
        self.steps, self.requests = list(steps), []

    async def stream_message(self, request):
        self.requests.append(request)
        step = self.steps.pop(0) if self.steps else "测试阶段已结束。"
        step = step(request) if callable(step) else step
        if __import__("inspect").isawaitable(step):
            step = await step
        if isinstance(step, str):
            blocks = [TextBlock(text=step)]
        else:
            name, arguments = step
            blocks = [ToolUseBlock(id=new_id("script"), name=name, input=arguments)]
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(role="assistant", content=blocks), usage=USAGE
        )


class AlphaModel:
    """Reads committed state to choose calls; never writes state or bypasses tools."""

    def __init__(
        self, store, *, keys=None, concurrency=3, fail=(), pause=False, hold=(), premature=False
    ):
        self.store = store
        self.keys = keys or ["company", "industry", "financial"]
        self.concurrency, self.fail, self.hold = concurrency, set(fail), set(hold)
        self.held_children = set()
        self.requests, self.planning_packets, self.calls, self.results = [], [], [], []
        self.child_calls, self.child_active, self.child_peak, self.child_cancelled = {}, 0, 0, []
        self.child_rounds = {}
        self.child_barrier, self.child_waiting, self.release_child = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        self.pause, self.paused, self.release_main = pause, asyncio.Event(), asyncio.Event()
        self.pause_on_task, self.task_paused, self.release_task = (
            None,
            asyncio.Event(),
            asyncio.Event(),
        )
        self.queue, self.active, self.topic = [], None, None
        self.seen_results, self.allowed_errors, self.observed_errors = set(), set(), []
        self.premature = premature

    async def mutation(self, action, **fields):
        return {
            "action": action,
            "operation_id": new_id("op"),
            "expected_revision": (await self.store.load()).revision,
            **fields,
        }

    @staticmethod
    def task(key, dependencies=(), criteria=None, kind="note"):
        return ResearchTask(
            id=key,
            title=key,
            dependencies=list(dependencies),
            acceptance_criteria=criteria or [key],
            required_artifact_kinds=[kind],
        )

    def objective(self):
        return ResearchObjective(
            project_id="Alpha",
            subject="AlphaTech",
            report_type="preliminary_research",
            requirements=["company", "industry", "financial", "summary"],
            deliverables=["report_draft"],
            required_sections=["研究摘要"],
        )

    def proposal(self):
        return PlanProposal(
            objective_revision=1,
            rationale="核查资料后形成研究摘要",
            tasks=[
                self.task("research", criteria=["company", "industry", "financial"]),
                self.task("draft", ["research"], ["summary"], "report_draft"),
            ],
        ).model_dump(mode="json")

    def patch(self, packet):
        draft = next(task for task in packet["plan"]["tasks"] if task["id"] == "draft")
        result = {
            "base_plan_revision": packet["plan"]["revision"],
            "objective_revision": packet["objective"]["revision"],
            "add_tasks": [self.task("overseas", ["research"], [FEEDBACK]).model_dump(mode="json")],
            "revise_tasks": [
                {
                    "task_id": "draft",
                    "expected_revision": draft["task_revision"],
                    "replacement": self.task(
                        "draft", ["research", "overseas"], ["summary"], "report_draft"
                    ).model_dump(mode="json"),
                }
            ],
            "reason": "增加海外重点，保留已完成主营与财务核查",
        }
        overseas = next(
            (task for task in packet["plan"]["tasks"] if task["id"] == "overseas"), None
        )
        if overseas:
            result["add_tasks"] = []
            result["revise_tasks"].append(
                {
                    "task_id": "overseas",
                    "expected_revision": overseas["task_revision"],
                    "replacement": self.task("overseas", ["research"], [FEEDBACK]).model_dump(
                        mode="json"
                    ),
                }
            )
        return result

    def checked(self, memory):
        return [
            key
            for key, item in memory.evidence_pool.items()
            if item.status == "source_checked"
            and not any(successor.supersedes == key for successor in memory.evidence_pool.values())
        ]

    async def observe(self, request):
        block = last_result(request)
        if block is None or block.tool_use_id in self.seen_results:
            return
        self.seen_results.add(block.tool_use_id)
        if block.is_error:
            assert any(value in block.content for value in self.allowed_errors), block.content
            self.observed_errors.append((block.content, (await self.store.load()).model_dump(mode="json")))
            self.queue = []
            return
        if block.result_metadata.get("dispatch_id"):
            self.results.append(json_result(block))

    async def action(self, stage, memory, plan):
        runtime_task = next(task for task in plan.tasks if task.id == self.active)
        evidence = self.checked(memory)
        if stage == "dispatch":
            return "dispatch_subagents", {
                "tasks": [
                    {
                        "task_id": key,
                        "instruction": f"调查 {key}，候选结论保留来源",
                        "context": f"AlphaTech {key}; 仅离线资料",
                    }
                    for key in self.keys
                ]
            }
        if stage.startswith("fetch:"):
            self.topic = stage.split(":", 1)[1]
            return "web_fetch", {"url": f"https://alphatech.invalid/{self.topic}"}
        if stage == "add_evidence":
            execution = next(
                item
                for item in reversed(list(memory.executions.values()))
                if item.tool_name == "web_fetch" and item.status == "committed"
            )
            return "research_memory", {
                "operation": (await self.mutation(
                    "add_evidence", source_id=execution.source_ids[0], statement=DOCS[self.topic]
                ))
            }
        if stage in {"verification", "finding_reason"}:
            key = next(reversed(memory.evidence_pool))
            return "research_memory", {
                "operation": (await self.mutation(
                    "add_reasoning",
                    evidence_ids=[key],
                    method="核对完整合成公告的主体、期间与口径",
                    result=DOCS[self.topic],
                    output="可追溯公开核查",
                    verification=stage == "verification",
                ))
            }
        if stage == "verify":
            return "research_memory", {
                "operation": (await self.mutation(
                    "verify_evidence",
                    evidence_id=next(reversed(memory.evidence_pool)),
                    level="source_checked",
                    method="source",
                    verification_step_id=next(reversed(memory.reasoning_chain)),
                    verification_note="与离线完整原文一致，不将单一来源升级为多来源验证",
                ))
            }
        if stage == "finding":
            return "research_memory", {
                "operation": (await self.mutation(
                    "add_conclusion",
                    statement=DOCS[self.topic],
                    evidence_ids=[next(reversed(memory.evidence_pool))],
                    step_ids=[next(reversed(memory.reasoning_chain))],
                ))
            }
        citations = " ".join(f"[E:{key}]" for key in evidence)
        if stage == "write":
            path = f"artifacts/{self.active}-{runtime_task.task_revision}.md"
            content = ("# 研究摘要\n" if self.active == "draft" else "# 核查记录\n") + "\n".join(
                [item.statement for item in memory.conclusions.values()] + [citations]
            )
            if self.results:
                content += "\n子代理整合：" + "; ".join(
                    item["summary"]
                    for item in self.results[-1]["results"]
                    if item["status"] == "completed"
                )
            return "write_file", {"path": path, "content": content}
        if stage == "memory":
            marker = (
                "<!-- 记录重要发现、来源、验证情况；不得将猜测当事实 -->"
                if self.active == "research"
                else "<!-- 用户提出的范围变化、研究取舍 -->"
            )
            return "edit_file", {
                "path": "MEMORY.md",
                "old_str": marker,
                "new_str": f"AlphaTech 研究发现 {citations}\n文件：artifacts/{self.active}-{runtime_task.task_revision}.md\n"
                + (
                    FEEDBACK
                    if self.active == "overseas"
                    else "主营、竞争及财务已核对原文，摘要尚待交付。"
                ),
            }
        if stage == "artifact":
            execution = next(
                item
                for item in reversed(list(memory.executions.values()))
                if item.tool_name == "write_file"
                and item.task_id == self.active
                and item.status == "committed"
            )
            content = (
                Path(memory.project.workspace_path)
                / f"artifacts/{self.active}-{runtime_task.task_revision}.md"
            ).read_text()
            fields = {
                "task_id": self.active,
                "task_revision": runtime_task.task_revision,
                "plan_revision": plan.revision,
                "execution_id": execution.id,
                "kind": "report_draft" if self.active == "draft" else "note",
                "title": self.active,
                "content": content,
                "criteria": runtime_task.acceptance_criteria,
                "evidence_ids": evidence,
                "finding_ids": list(memory.conclusions),
            }
            if self.active == "draft":
                fields.update(
                    sections=["研究摘要"],
                    input_artifact_ids=[
                        key
                        for task in plan.tasks
                        if task.id in runtime_task.dependencies
                        for key in task.artifact_ids
                    ],
                )
            return "research_project", {
                "operation": (await self.mutation("submit_artifact", artifact=fields))
            }
        if stage in {"complete", "premature"}:
            return "research_project", {
                "operation": (await self.mutation(
                    "complete_task", task_id=self.active, task_revision=runtime_task.task_revision
                ))
            }
        raise AssertionError(stage)

    async def child(self, request):
        packet = json.loads(request.messages[0].text)
        key = packet["task_id"]
        call = self.child_rounds.get(packet["output_directory"], 0) + 1
        self.child_rounds[packet["output_directory"]] = call
        self.child_calls[key] = self.child_calls.get(key, 0) + 1
        self.child_active += 1
        self.child_peak = max(self.child_peak, self.child_active)
        try:
            if self.child_peak >= min(self.concurrency, len(self.keys)):
                self.child_barrier.set()
            await asyncio.wait_for(self.child_barrier.wait(), timeout=3)
            await asyncio.sleep(0.005)
            if key in self.fail:
                raise RuntimeError(f"Expected offline child failure: {key}")
            if call == 1:
                name, fields = (
                    "read_file",
                    {
                        "path": getattr(
                            self, "external_private_path", packet["parent_workspace"] + "/MEMORY.md"
                        )
                    },
                )
            elif call == 2:
                name, fields = (
                    "write_file",
                    {
                        "path": packet["parent_workspace"] + "/MEMORY.md",
                        "content": "CHILD_FORBIDDEN",
                    },
                )
            elif call == 3:
                name, fields = (
                    "research_memory",
                    {
                        "operation": {
                            "action": "update_task",
                            "task_id": "research",
                            "status": "completed",
                        }
                    },
                )
            elif call == 4:
                name, fields = (
                    "web_fetch",
                    {"url": "https://alphatech.invalid/" + (key if key in DOCS else "company")},
                )
            elif call == 5:
                name, fields = (
                    "write_file",
                    {
                        "path": "candidate.md",
                        "content": f"{key} candidate: " + last_result(request).content,
                    },
                )
            else:
                if key in self.hold:
                    self.held_children.add(key)
                    # Cancellation tests target in-flight model calls, not the
                    # intervening database writes. Observe every intended waiter.
                    if len(self.held_children) >= min(self.concurrency, len(self.hold)):
                        self.child_waiting.set()
                    await self.release_child.wait()
                name, fields = (
                    "submit_subagent_result",
                    {
                        "summary": f"{key} candidate; source alphatech.invalid/{key}; 主级复核待办",
                        "output_paths": ["candidate.md"],
                    },
                )
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(
                    role="assistant",
                    content=[ToolUseBlock(id=new_id("child"), name=name, input=fields)],
                ),
                usage=USAGE,
            )
        except asyncio.CancelledError:
            self.child_cancelled.append(key)
            raise
        finally:
            self.child_active -= 1

    async def stream_message(self, request):
        self.requests.append(request)
        names = {tool["name"] for tool in request.tools}
        if "submit_subagent_result" in names:
            async for event in self.child(request):
                yield event
            return
        if names in ({"submit_plan_proposal"}, {"submit_plan_patch"}):
            packet = json.loads(request.messages[0].text)
            self.planning_packets.append(packet)
            name = next(iter(names))
            fields = self.proposal() if name == "submit_plan_proposal" else self.patch(packet)
        else:
            (await self.observe(request))
            memory = (await self.store.load())
            plan = memory.plans.get(memory.research_state.current_plan_id or "")
            if self.pause_on_task and memory.research_state.current_task_id == self.pause_on_task:
                self.pause_on_task = None
                self.task_paused.set()
                await self.release_task.wait()
                memory = (await self.store.load())
                plan = memory.plans[memory.research_state.current_plan_id]
            if (
                plan
                and self.pause
                and next(task for task in plan.tasks if task.id == "research").status == "completed"
            ):
                self.pause = False
                self.paused.set()
                await self.release_main.wait()
                memory = (await self.store.load())
                plan = memory.plans[memory.research_state.current_plan_id]
            if memory.project is None:
                user_id = next(
                    key
                    for key, source in reversed(list(memory.sources.items()))
                    if source.kind == "user"
                )
                name, fields = (
                    "research_project",
                    {
                        "operation": (await self.mutation(
                            "start",
                            objective=self.objective().model_dump(mode="json"),
                            user_source_ids=[user_id],
                        ))
                    },
                )
            elif memory.project.status == "suspended":
                name, fields = "research_project", {"operation": (await self.mutation("resume"))}
            elif plan is None:
                name, fields = "planner", {"objective": "AlphaTech 初步研究"}
            elif memory.research_state.replan_required:
                self.queue = []
                name, fields = "replanner", {"reason": FEEDBACK}
            elif self.queue:
                name, fields = (await self.action(self.queue.pop(0), memory, plan))
            elif any(task.status == "ready" for task in plan.tasks):
                selected = next(task for task in plan.tasks if task.status == "ready")
                self.active = selected.id
                topics = (
                    ["company", "industry", "financial"]
                    if self.active == "research"
                    else ["overseas"]
                    if self.active == "overseas"
                    else []
                )
                self.queue = ["dispatch"] if self.active == "research" else []
                if self.active == "research" and self.premature:
                    self.queue.insert(0, "premature")
                for topic in topics:
                    self.queue += [
                        f"fetch:{topic}",
                        "add_evidence",
                        "verification",
                        "verify",
                        "finding_reason",
                        "finding",
                    ]
                self.queue += ["write"] + (["memory"] if topics else []) + ["artifact", "complete"]
                name, fields = (
                    "research_project",
                    {
                        "operation": (await self.mutation(
                            "claim_task", task_id=self.active, task_revision=selected.task_revision
                        ))
                    },
                )
            elif memory.project.status != "completed":
                name, fields = "research_project", {"operation": (await self.mutation("finalize"))}
            else:
                yield ApiMessageCompleteEvent(
                    message=ConversationMessage(
                        role="assistant",
                        content=[
                            TextBlock(
                                text="AlphaTech 合成研究摘要已验收。"
                                + " ".join(f"[E:{key}]" for key in self.checked(memory))
                            )
                        ],
                    ),
                    usage=USAGE,
                )
                return
        self.calls.append((name, fields))
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(
                role="assistant", content=[ToolUseBlock(id=new_id("main"), name=name, input=fields)]
            ),
            usage=USAGE,
        )


class Lab:
    def __init__(self, tmp_path):
        self.root, self.bundles = tmp_path, []
        self.background = []

    def start(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.background.append(task)
        return task

    async def create(
        self, *, model_factory=AlphaModel, session="a" * 12, concurrency=3, restore=None, **options
    ):
        store = ResearchStore(self.root, session, root=self.root / "state")
        model = model_factory(store, concurrency=concurrency, **options)
        bundle = await build_runtime(
            cwd=str(self.root),
            session_id=session,
            api_client=model,
            connect_mcp=False,
            research_store_override=store,
            restore_messages=restore,
            max_turns=100,
            settings_override=Settings(
                model="offline-fixture",
                context_window_tokens=1000000,
                permission={"allowed_tools": ["write_file", "edit_file", "bash"]},
                research_memory={
                    "workspace_root": str(self.root / "workspaces"),
                    "subagent_max_concurrency": concurrency,
                },
            ),
        )
        fetch = OfflineFetch()
        # Explicit fixture substitution; production registration must reject collisions.
        bundle.tool_registry.unregister(fetch.name)
        bundle.tool_registry.register(fetch)
        self.bundles.append(bundle)
        return bundle, store, model, fetch

    async def close(self):
        for task in self.background:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.background, return_exceptions=True)
        for bundle in self.bundles:
            await close_runtime(bundle)


async def await_point(event, running, *, timeout=60):
    """Observe a barrier or surface the driver's failure, allowing coverage overhead."""
    waiter = asyncio.create_task(event.wait())
    try:
        done, _ = await asyncio.wait(
            (waiter, running), timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if waiter in done:
            return
        if running in done:
            await running
            raise AssertionError("Main driver ended before the expected synchronization point")
        raise TimeoutError("Main driver did not reach the synchronization point")
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def collect(engine, prompt=REQUEST):
    events = [event async for event in engine.submit_message(prompt)]
    errors = [
        event
        for event in events
        if isinstance(event, ErrorEvent)
        or isinstance(event, ToolExecutionCompleted)
        and event.is_error
    ]
    assert not errors, errors
    return events
