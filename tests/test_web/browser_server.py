"""Isolated Playwright server. Only the model transport is replaced for tests."""

from __future__ import annotations

import asyncio
import os
import tempfile
import json
import re
import sys
import shlex
import shutil
from pathlib import Path

import uvicorn

import researchx.runtime as agent_runtime
import researchx.web.app as web_app
from researchx.api.client import ApiMessageCompleteEvent, ApiTextDeltaEvent
from researchx.api.usage import UsageSnapshot
from researchx.config import Settings, save_settings
from researchx.engine.messages import ConversationMessage, TextBlock, ToolUseBlock
from research_model import ResearchModel


class BrowserModel:
    def __init__(self):
        self.research = None

    async def close(self):
        pass

    async def stream_message(self, request):
        if any(message.text == "长篇研究" for message in request.messages):
            await asyncio.sleep(3600)
        last = next(message for message in reversed(request.messages) if message.content)
        user_text = next(
            (
                message.text
                for message in reversed(request.messages)
                if message.role == "user" and message.text
            ),
            "",
        )
        if user_text in {"开展测试研究", "慢速研究公司 A", "改为研究公司 B"}:
            if self.research is None:
                context = next(
                    message.runtime_context
                    for message in reversed(request.messages)
                    if message.runtime_context
                )
                data = json.loads(
                    re.search(r"<research_memory>\n(.*?)\n</research_memory>", context, re.S).group(
                        1
                    )
                )
                self.research = ResearchModel(
                    WORKSPACE,
                    data["session_id"],
                    slow=user_text == "慢速研究公司 A",
                    title="公司 B 简要研究" if user_text == "改为研究公司 B" else "公司 A 简要研究",
                    step_delay=0.25,
                )
            async for event in self.research.stream_message(request):
                yield event
            return
        if user_text == "查看执行过程":
            if last.role == "user" and last.text:
                text = "我先读取两份资料，再整理结论。"
                yield ApiTextDeltaEvent(text)
                message = ConversationMessage(
                    role="assistant",
                    content=[
                        TextBlock(text=text),
                        ToolUseBlock(
                            id="process-read-ok", name="read_file", input={"path": "report.txt"}
                        ),
                        ToolUseBlock(
                            id="process-read-fail", name="read_file", input={"path": "missing.txt"}
                        ),
                    ],
                )
            else:
                # Keep the process running long enough to exercise disclosure controls.
                await asyncio.sleep(3)
                text = "已整理资料；第二份文件不可用。"
                yield ApiTextDeltaEvent(text)
                message = ConversationMessage(role="assistant", content=[TextBlock(text=text)])
            yield ApiMessageCompleteEvent(
                message=message, usage=UsageSnapshot(input_tokens=10, output_tokens=5)
            )
            return
        if last.text.startswith("导出固定验收产物"):
            scripts = (
                Path(__file__).resolve().parents[2]
                / "src/researchx/skills/bundled/research-report-digest/scripts"
            )
            python = shlex.quote(
                str(Path(sys.executable).parent.resolve() / Path(sys.executable).name)
            )
            process = f"{python} {shlex.quote(str(scripts / 'digest_reports.py'))} --input fixtures/digest.json --output browser-artifacts/computed.json"
            export = f"{python} {shlex.quote(str(scripts / 'export_report.py'))} --input browser-artifacts/computed.json --output-dir browser-artifacts"
            command = process + " && " + export
            message = ConversationMessage(
                role="assistant", content=[ToolUseBlock(name="bash", input={"command": command})]
            )
        elif last.text == "并行确认测试":
            message = ConversationMessage(
                role="assistant",
                content=[
                    ToolUseBlock(
                        name="write_file", input={"path": f"parallel-{i}.txt", "content": "测试"}
                    )
                    for i in range(4)
                ],
            )
        elif last.text == "请写入测试文件":
            message = ConversationMessage(
                role="assistant",
                content=[
                    ToolUseBlock(
                        name="write_file", input={"path": "approval.txt", "content": "测试"}
                    ),
                ],
            )
        elif last.text == "请提问":
            message = ConversationMessage(
                role="assistant",
                content=[
                    ToolUseBlock(
                        name="ask_user_question", input={"question": "你希望研究哪个时间范围？"}
                    ),
                ],
            )
        else:
            text = "这是浏览器测试回复。\n\n| 项目 | 状态 |\n| --- | --- |\n| 对话链路 | 已接通 |"
            if last.text == "请显示测试标记":
                text = '<script>window.__unsafe=true</script>\n<img src="bad" onerror="window.__unsafe=true">\n\n安全内容\n\n[危险链接](javascript:alert(1))'
            if last.text == "请慢慢回复":
                yield ApiTextDeltaEvent("这是部分回复")
                await asyncio.sleep(3600)
            for chunk in (text[:8], text[8:18], text[18:]):
                yield ApiTextDeltaEvent(chunk)
                await asyncio.sleep(0.1)
            message = ConversationMessage(role="assistant", content=[TextBlock(text=text)])
        yield ApiMessageCompleteEvent(
            message=message,
            usage=UsageSnapshot(
                input_tokens=10,
                output_tokens=5,
                cache_read_input_tokens=0,
                cache_observed_input_tokens=10,
            ),
        )


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="researchx-browser-") as directory:
        root = Path(directory)
        # Isolate fake-model credentials from the desktop keyring and D-Bus prompts.
        os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"
        os.environ["RESEARCHX_CONFIG_DIR"] = str(root / "config")
        os.environ["RESEARCHX_DATA_DIR"] = str(root / "data")
        for name in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "RESEARCHX_OPENAI_API_KEY",
            "RESEARCHX_ANTHROPIC_API_KEY",
            "RESEARCHX_PROFILE",
        ):
            os.environ.pop(name, None)
        # Synthetic model names have no provider context-window entry.
        save_settings(Settings(context_window_tokens=200_000))
        (root / "workspace").mkdir()
        WORKSPACE = root / "workspace"
        Path.home = classmethod(lambda cls: WORKSPACE / "home")
        shutil.copytree(
            Path(__file__).parents[1] / "fixtures/research_skills", WORKSPACE / "fixtures"
        )
        (WORKSPACE / "report.txt").write_text("测试资料：营业收入同比增长。", encoding="utf-8")
        agent_runtime._resolve_api_client_from_settings = lambda settings: BrowserModel()
        web_app._resolve_api_client_from_settings = lambda settings: BrowserModel()
        app = web_app.create_app(str(root / "workspace"))
        # Deterministic projection for UI states that cannot all be reached at
        # once in a real run. Delivery still goes through the real SSE route.
        from researchx.state.store import ResearchStore
        from researchx.state.models import SourceUsage
        import researchx.web.runtime as browser_runtime

        original_build_runtime = browser_runtime.build_runtime

        async def build_fixture_runtime(**kwargs):
            bundle = await original_build_runtime(**kwargs)
            store = bundle.engine.tool_metadata["research_store"]
            path = WORKSPACE / "report.txt"
            await store.capture(
                origin_id="public-browser-fixture", kind="file", content=path.read_text(),
                locator=str(path), title="公开测试资料",
                usage=SourceUsage(category="public", licence_status="pending",
                                  model_use_allowed=True, internal_export_allowed=True,
                                  completeness="full"),
            )
            return bundle

        browser_runtime.build_runtime = build_fixture_runtime

        progress_fixtures = {}
        original_progress = ResearchStore.progress

        async def projected_progress(store, memory=None):
            return progress_fixtures.get(store.session_id) or await original_progress(store, memory)

        ResearchStore.progress = projected_progress

        @app.post("/__test/project-progress/{session_id}")
        async def project_progress(session_id: str, progress: dict):
            await app.state.workspace.record(session_id)
            progress_fixtures[session_id] = progress
            connection = app.state.workspace.connections.get(session_id)
            if connection:
                await connection.emit("research_progress", progress=progress)
            return {"ok": True}

        @app.post("/__test/stream-delta/{session_id}")
        async def stream_delta(session_id: str, payload: dict):
            connection = app.state.workspace.connections[session_id]
            await connection.emit("delta", **payload)
            return {"ok": True}

        @app.post("/__test/citation-layout")
        async def citation_layout(profile_id: str):
            from uuid import uuid4
            from researchx.state.store import ResearchStore

            record = await app.state.workspace.store.create(profile_id)
            store = ResearchStore(WORKSPACE, record["session_id"])

            async def update(action, **fields):
                return await store.apply(
                    dict(
                        action=action,
                        operation_id=uuid4().hex,
                        expected_revision=(await store.load()).revision,
                        **fields,
                    )
                )

            user = await store.capture(origin_id="user-layout", kind="user", content="研究光伏行业")
            await update("set_context", goal="光伏研究", user_source_ids=[user.id])
            (
                await update(
                    "create_plan",
                    title="光伏行业景气度与供需变化分析" * 5,
                    tasks=["收集国内新增装机及组件出口月度量价数据" * 4],
                )
            )
            task = next(iter((await store.load()).plans.values())).tasks[0]
            (await update("update_task", task_id=task.id, status="in_progress"))
            keys = []
            for index, kind in enumerate(["mcp", "web", "tool", "search"]):
                source = await store.capture(
                    origin_id=f"source-{index}",
                    kind=kind,
                    title="外部网页资料与光伏行业最新供需数据" * 5
                    if kind in {"web", "search"}
                    else "内部数据",
                    locator=f"https://example.org/report/{index}",
                    content="资料",
                )
                keys.append(
                    (await update("add_evidence", source_id=source.id, statement="资料"))[
                        "evidence_id"
                    ]
                )
            raw = "；".join(f"结论{index}[E:{key}]" for index, key in enumerate(keys))
            raw += f"；重复[E:{keys[1]}]"
            rendered, frozen = await store.render_answer(raw, "layout-answer")
            record["messages"] = [
                ConversationMessage(
                    role="assistant", content=[TextBlock(text=rendered)], research_citations=frozen
                ).model_dump(mode="json")
            ]
            record["display_messages"] = [
                dict(
                    id="layout-row",
                    role="assistant",
                    text=rendered,
                    turn_id="layout",
                    turn_status="completed",
                    phase="final",
                )
            ]
            (await app.state.workspace.store.write(record))
            return {"session_id": record["session_id"]}

        @app.post("/__test/conflict-progress")
        async def conflict_progress(profile_id: str, outcome: str = "unresolved"):
            from uuid import uuid4
            from researchx.state.store import ResearchStore

            seeded = await citation_layout(profile_id)
            store = ResearchStore(WORKSPACE, seeded["session_id"])

            async def update(action, **fields):
                return await store.apply(
                    dict(
                        action=action,
                        operation_id=uuid4().hex,
                        expected_revision=(await store.load()).revision,
                        **fields,
                    )
                )

            evidence_ids = list((await store.load()).evidence_pool)[:2]
            step = (
                await update(
                    "add_reasoning",
                    evidence_ids=evidence_ids,
                    method="比较两份原始资料",
                    result="数字口径存在分歧",
                    output="需要澄清口径",
                )
            )["step_id"]
            cid = (
                await update(
                    "add_conflict",
                    question="两份公告的营收数字为何不同？",
                    kind="scope",
                    sides=[
                        dict(statement=f"公告{index}口径", evidence_ids=[key], step_ids=[step])
                        for index, key in enumerate(evidence_ids)
                    ],
                )
            )["conflict_id"]
            arbitration, _ = await store.begin_investigation(cid)
            report = dict(
                outcome=outcome,
                statement="需按公告统计口径分别判断",
                rationale="已比对双方原文及期间",
                evidence_ids=evidence_ids,
                step_ids=[step],
                remaining_gaps=["尚缺统计范围说明"] if outcome == "unresolved" else [],
                assessments=[
                    dict(
                        evidence_id=key,
                        originality="已检查原始出处",
                        directness="原表直接列示",
                        scope_match="口径需要区分",
                        timing_and_corrections="已检查公告时点",
                        independence="保留各自出处",
                        reproducibility="原文可复核",
                    )
                    for key in evidence_ids
                ],
            )
            from researchx.state.models import ArbitrationDecision

            (
                await store.finish_investigation(
                    arbitration.id, report=ArbitrationDecision.model_validate(report)
                )
            )
            (
                await update(
                    "resolve_conflict",
                    conflict_id=cid,
                    arbitration_id=arbitration.id,
                    decision=report,
                )
            )
            (
                await update(
                    "update_task",
                    task_id=(await store.load()).research_state.current_task_id,
                    status="completed",
                    completion_note="核查已完成",
                )
            )
            return seeded

        uvicorn.run(app, host="127.0.0.1", port=8765)
