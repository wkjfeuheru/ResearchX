"""Opt-in real-provider acceptance of four skill plugins via the web transport.

Run with --profile ID --output /tmp/skill-eval. Credentials remain in temporary
config; evaluation files contain only fixtures, generated results and audits.
"""

from __future__ import annotations

import argparse
from researchx.services.execution.async_timeout import timeout as async_timeout
import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
import uvicorn
from tests.test_web.http_sse import sse_connection

from researchx.auth.storage import store_credential
from researchx.config import Settings, save_settings
from researchx.state.store import ResearchStore
from researchx.web.app import create_app
from researchx.web.catalog import profile_settings
from importlib import import_module

SKILL_FUNCTIONS = {
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
RESULT_TYPES = {}
FUNCTIONS = {}
for kind, (plugin, script, function, result_type) in SKILL_FUNCTIONS.items():
    package = "report-generation" if kind == "deep" else "analysis-modeling"
    module = import_module(f"researchx.skills.bundled.{plugin}.scripts.{script}")
    RESULT_TYPES[kind] = getattr(module, result_type)
    FUNCTIONS[kind] = getattr(module, function)


async def run(args):
    from researchx.storage.database import database_lifespan
    async with database_lifespan():
        return await _run(args)


async def _run(args):
    selected = profile_settings(args.profile)
    credential = selected.resolve_auth().value
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    cwd = output / "workspace"
    cwd.mkdir(exist_ok=True)
    fixtures = Path(__file__).parents[1] / "fixtures" / "research_skills"
    shutil.copytree(fixtures, cwd / "fixtures", dirs_exist_ok=True)
    base = f"http://127.0.0.1:{args.port}"
    audit = []
    with TemporaryDirectory(prefix="skill-eval-config-") as config:
        os.environ["RESEARCHX_CONFIG_DIR"] = config
        os.environ["RESEARCHX_DATA_DIR"] = str(output / "data")
        profile = selected.resolve_profile()[1].model_copy(update={"credential_slot": "eval"})
        save_settings(
            Settings(
                active_profile="eval",
                profiles={"eval": profile},
                api_format=selected.api_format,
                model=selected.model,
                base_url=selected.base_url,
                provider=selected.provider,
                web=selected.web,
                mcp_servers={},
            )
        )
        store_credential("profile:eval", "api_key", credential, use_keyring=False)
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(str(cwd)), host="127.0.0.1", port=args.port, log_level="warning"
            )
        )
        task = asyncio.create_task(server.serve())
        try:
            async with async_timeout(20):
                while not server.started:
                    if task.done():
                        await task
                    await asyncio.sleep(0.05)
            async with httpx.AsyncClient(base_url=base, timeout=60) as client:
                r = await client.post("/api/sessions", json={"profile_id": "eval"})
                r.raise_for_status()
                sid = r.json()["session_id"]
                pdf = await client.post(
                    f"/api/sessions/{sid}/attachments",
                    files={
                        "files": (
                            "annual-report.pdf",
                            (fixtures / "annual-report.pdf").read_bytes(),
                        )
                    },
                )
                pdf.raise_for_status()
                attachment_id = pdf.json()["items"][0]["id"]
                names = [
                    (
                        "financial",
                        "financial-statement-analysis",
                        "使用本轮上传的两页文字PDF，语义提取科目、核对单位和页码；不要直接复制financial.json。简化ROE应使用期初90与期末100万元。",
                    ),
                    (
                        "monitor",
                        "company-event-monitor",
                        "读取fixtures/events.txt作为固定公告材料；窗口截止2026-10-05T12:00:00+08:00，输出事件、文本情绪与影响评分。",
                    ),
                    (
                        "digest",
                        "research-report-digest",
                        "读取fixtures/broker.txt作为研报原文，提取观点、盈利预测、目标价及评级，不虚构前次评级。",
                    ),
                    (
                        "deep",
                        "deep-investment-report",
                        "整合本会话已有三项产物，使用fixtures/deep.json中的全部显式预测假设（三种情景固定同值用于核对计算）。自主组织八个章节，引用原始材料并写入prior_results。按原输入最新完整2025年财报预测2026、2027、2028，不添加自主评级或目标价。",
                    ),
                ]
                for kind, name, objective in names:
                    request_id = uuid4().hex
                    start = time.monotonic()
                    print(json.dumps({"kind": kind, "state": "start", "session": sid}), flush=True)
                    question = (
                        f"调用 {name} Skill 完成验收并导出四种文件。{objective}\n"
                        "这是用户明确指定的合成固定材料验收，样例制造603999/SSE是测试标识，不要求现实证券身份。"
                        "所有输入均为制造业非金融虚构公司，仅计算测试。无需联网、不澄清现实公司。"
                        "资料截止固定2026-10-05T12:00:00+08:00，缺失信息记录null与原因；登记来源、计算证据和结论。"
                        "可读写本会话目录，使用skill返回的Python解释器，执行技能自己的业务脚本，再执行export_report.py导出，不只解释方法。"
                    )
                    events = []
                    async with async_timeout(args.turn_timeout):
                        async with sse_connection(
                            client,
                            sid,
                            max_event_bytes=8000000,
                        ) as ws:
                            assert json.loads(await ws.recv())["type"] == "ready"
                            await ws.send(
                                json.dumps(
                                    {
                                        "type": "submit",
                                        "request_id": request_id,
                                        "text": question,
                                        "attachment_ids": [attachment_id]
                                        if kind == "financial"
                                        else [],
                                    },
                                    ensure_ascii=False,
                                )
                            )
                            while True:
                                event = json.loads(await ws.recv())
                                if event["type"] in {
                                    "prompt",
                                    "error",
                                    "done",
                                    "research_progress",
                                }:
                                    events.append(event)
                                    print(
                                        json.dumps(
                                            {
                                                "kind": kind,
                                                "state": event["type"],
                                                "detail": event.get(
                                                    "message", event.get("progress", {})
                                                ),
                                            },
                                            ensure_ascii=False,
                                        ),
                                        flush=True,
                                    )
                                if event["type"] == "prompt":
                                    answer = (
                                        "allow"
                                        if event["kind"] != "question"
                                        else "按给定合成材料继续，本验收无需联网和真实身份确认；数据不足明确记录缺口。"
                                    )
                                    await ws.send(
                                        json.dumps(
                                            {
                                                "type": "response",
                                                "request_id": request_id,
                                                "prompt_id": event["prompt_id"],
                                                "answer": answer,
                                            },
                                            ensure_ascii=False,
                                        )
                                    )
                                if event["type"] == "done":
                                    break
                    artifacts = (await client.get(f"/api/sessions/{sid}/artifacts")).json()["items"]
                    candidates = [a for a in artifacts if a["kind"] == kind and a["type"] == "json"]
                    item = {
                        "kind": kind,
                        "seconds": round(time.monotonic() - start, 2),
                        "failed": event.get("failed"),
                        "events": events,
                        "artifacts": artifacts,
                        "checks": {},
                    }
                    if candidates:
                        artifact = candidates[-1]
                        payload = (
                            await client.get(
                                f"/api/sessions/{sid}/artifacts/{artifact['id']}/download"
                            )
                        ).json()
                        (output / f"{kind}.json").write_text(
                            json.dumps(payload, ensure_ascii=False, indent=2)
                        )
                        model = RESULT_TYPES[kind].model_validate(payload)
                        recalc = FUNCTIONS[kind](model.model_copy(deep=True))
                        item["checks"]["schema_and_status"] = not event.get(
                            "failed"
                        ) and recalc.status in {"complete", "partial"}
                        item["checks"]["four_formats"] = {
                            a["type"] for a in artifacts if a["kind"] == kind
                        } == {"json", "md", "docx", "xlsx"}
                        if kind == "financial":
                            ratio = next(
                                row for row in recalc.ratios if row["metric"] == "gross_margin"
                            )
                            item["checks"]["gross_margin"] = float(ratio["value"]) == 0.4
                            item["checks"]["balance"] = any(
                                row["check"] == "balance_sheet" and row["state"] == "pass"
                                for row in recalc.checks
                            )
                            item["checks"]["actual_pdf_pages"] = {
                                ref.page
                                for p in recalc.periods
                                for a in p.items.values()
                                for ref in a.references
                            } == {1, 2}
                        elif kind == "monitor":
                            item["checks"]["event_score"] = (
                                len(recalc.events) == 1 and recalc.events[0].impact.value == 1
                            )
                        elif kind == "digest":
                            item["checks"]["prediction_and_rating"] = (
                                recalc.reports[0].predictions[0].year == 2026
                                and recalc.reports[0].rating_change is None
                            )
                        else:
                            item["checks"]["scenarios_and_values"] = (
                                len(recalc.forecasts) == 9
                                and len(recalc.sensitivity) == 27
                                and float(recalc.forecasts[0]["values"]["revenue"]) == 1100000
                            )
                    else:
                        item["checks"]["artifact_created"] = False
                    state = (await ResearchStore(cwd, sid).load())
                    item["checks"]["research_memory"] = bool(state.sources and state.evidence_pool)
                    record = (await client.get(f"/api/sessions/{sid}")).json()
                    (output / f"{kind}-session.json").write_text(
                        json.dumps(record, ensure_ascii=False, indent=2)
                    )
                    audit.append(item)
                    (output / "audit.json").write_text(
                        json.dumps(audit, ensure_ascii=False, indent=2)
                    )
                    print(
                        json.dumps({"kind": kind, "checks": item["checks"]}, ensure_ascii=False),
                        flush=True,
                    )
        finally:
            server.should_exit = True
            await task
    if any(not all(item["checks"].values()) for item in audit):
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--turn-timeout", type=int, default=900)
    asyncio.run(run(parser.parse_args()))
