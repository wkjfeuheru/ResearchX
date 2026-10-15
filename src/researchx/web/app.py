"""FastAPI app for a local, single-user financial research workspace."""

from __future__ import annotations
from typing import Callable, Awaitable, AsyncIterator
from starlette.types import ASGIApp, Scope, Receive, Send, Message
from starlette.responses import Response
from starlette.datastructures import UploadFile

import asyncio
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from anyio import CancelScope

from researchx.api.client import ApiMessageCompleteEvent, ApiMessageRequest
from researchx.auth.manager import AuthManager
from researchx.engine.messages import ConversationMessage
from researchx.state.store import ResearchStore
from researchx.state.deliveries import DeliveryService
from researchx.state.errors import ResearchError
from researchx.runtime import _resolve_api_client_from_settings
from researchx.web.catalog import (
    models_list,
    profile_settings,
    save_model,
    skills_list,
    skill_detail,
    toggle_skill,
)
from researchx.web.models import (
    ModelInput,
    SessionInput,
    SkillToggle,
    CommandRequest,
    ReviewRequest,
)
from researchx.web.runtime import SessionController
from researchx.web.redaction import Redactor
from researchx.web.session_view import session_view
from researchx.web.workspace import WebWorkspace
from researchx.web.events import EventChannel, SessionEventResponse, encode_event
from researchx.workspace.documents import MAX_DOCUMENT_BYTES
from researchx.workspace.session_files import SessionFiles

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class AttachmentBodyLimit:
    """Bound multipart body bytes before Starlette spools uploads to disk."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or not scope["path"].endswith("/attachments")
        ):
            return await self.app(scope, receive, send)
        limit = 10 * MAX_DOCUMENT_BYTES + 1024 * 1024
        seen = 0

        async def bounded_receive() -> Message:
            nonlocal seen
            message = await receive()
            seen += len(message.get("body", b""))
            if seen > limit:
                raise HTTPException(413, "附件请求超过大小限制")
            return message

        return await self.app(scope, bounded_receive, send)


def local_host(value: str) -> bool:
    try:
        return urlsplit(f"//{value}").hostname in LOCAL_HOSTS
    except ValueError:
        return False


def allowed_origin(origin: str | None, host: str) -> bool:
    if origin is None:
        return True  # Non-browser HTTP clients; browser Origin is checked when present.
    try:
        url = urlsplit(origin)
        return (
            url.scheme in {"http", "https"}
            and url.hostname in LOCAL_HOSTS
            and (url.netloc == host or url.port == 5173)
        )
    except ValueError:
        return False


def create_app(cwd: str | None = None, static_dir: Path | None = None) -> FastAPI:
    from researchx.storage.database import Database, bind_database

    database = Database()
    workspace = WebWorkspace(cwd or str(Path.cwd()))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            await database.check()
            with bind_database(database):
                from researchx.storage.database import workspace_id
                from researchx.storage.run_records import pause_unfinished_runs

                await pause_unfinished_runs(workspace_id(workspace.cwd))
                try:
                    yield
                finally:
                    for connection in list(workspace.connections.values()):
                        await connection.close()
                    for connection in list(workspace.active_runs.values()):
                        await connection.close()
                    workspace.connections.clear()
                    workspace.active_runs.clear()
        finally:
            await database.close()

    app = FastAPI(title="ResearchX 投研工作台", lifespan=lifespan)
    app.add_middleware(AttachmentBodyLimit)
    app.state.workspace = workspace
    app.state.database = database

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        host = request.headers.get("host", "")
        if not local_host(host) or not allowed_origin(request.headers.get("origin"), host):
            return JSONResponse({"detail": "仅允许本机工作台访问"}, status_code=403)
        with bind_database(database):
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        if not response.headers.get("content-type", "").startswith("text/event-stream"):
            response.headers["Cache-Control"] = (
                "no-store" if request.url.path.startswith("/api") else "no-cache"
            )
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic's default payload echoes inputs, including API keys.
        return JSONResponse({"detail": "请求字段无效，请检查表单内容"}, status_code=422)

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        from researchx.storage.database import DatabaseConfigurationError

        try:
            await database.check()
        except DatabaseConfigurationError as exc:
            raise HTTPException(503, str(exc)) from None
        return {"status": "ok", "busy": workspace.busy()}

    @app.get("/api/models")
    async def get_models() -> dict[str, object]:
        return dict((models_list()))

    @app.post("/api/models", status_code=201)
    async def add_model(data: ModelInput) -> dict[str, object]:
        return {"id": save_model(data)}

    @app.put("/api/models/{profile_id}")
    async def edit_model(profile_id: str, data: ModelInput) -> dict[str, object]:
        return {"id": save_model(data, profile_id)}

    @app.post("/api/models/{profile_id}/activate")
    async def activate_model(profile_id: str) -> dict[str, object]:
        profile_settings(profile_id)
        AuthManager().use_profile(profile_id)
        return {"ok": True}

    @(app.delete("/api/models/{profile_id}/credential"))
    async def clear_credential(profile_id: str) -> dict[str, object]:
        profile_settings(profile_id)
        AuthManager().clear_profile_credential(profile_id)
        return {"ok": True}

    @(app.delete("/api/models/{profile_id}"))
    async def delete_model(profile_id: str) -> dict[str, object]:
        profile_settings(profile_id)
        if any(
            r["profile_id"] == profile_id
            for r in (await workspace.store.list_snapshots(workspace.cwd, limit=1000000))
        ):
            raise HTTPException(409, "此配置正在被会话使用，请先切换会话的模型")
        manager = AuthManager()
        try:
            manager.remove_profile(profile_id)
        except ValueError:
            raise HTTPException(409, "默认配置或内置配置不能删除，请先切换默认配置") from None
        # Custom slots are owned by their profile; legacy shared slots are kept.
        if profile_id.startswith("web-"):
            from researchx.auth.storage import clear_provider_credentials

            clear_provider_credentials(f"profile:{profile_id}")
        return {"ok": True}

    @app.post("/api/models/{profile_id}/test")
    async def test_model(profile_id: str) -> dict[str, object]:
        settings = profile_settings(profile_id)
        try:
            (settings.resolve_auth())
        except ValueError:
            raise HTTPException(400, "请先配置此模型的凭据") from None
        client = _resolve_api_client_from_settings(settings)
        try:

            async def probe() -> None:
                complete = False
                from researchx.services.context.budget import checked_request

                async for event in client.stream_message(
                    (
                        checked_request(
                            client,
                            ApiMessageRequest(
                                audit_cwd=str(workspace.cwd),
                                audit_session_id="model-probe",
                                model=settings.model,
                                max_tokens=64,
                                context_window_tokens=settings.context_window_tokens,
                                messages=[ConversationMessage.from_user_text("Reply with OK.")],
                            ),
                        )
                    )
                ):
                    if isinstance(event, ApiMessageCompleteEvent):
                        complete = True
                if not complete:
                    raise ValueError("模型未返回完整响应")

            await asyncio.wait_for((probe()), timeout=30)
            return {"ok": True, "message": "连接成功，模型可正常响应"}
        except asyncio.TimeoutError:
            return {"ok": False, "message": "连接超时，请检查接口地址和网络"}
        except Exception as exc:  # noqa: BLE001 -- redact upstream exception bodies
            from researchx.api.errors import AuthenticationFailure, RateLimitFailure
            from researchx.services.context.budget import ContextBudgetError

            message = "连接失败，请检查接口地址、模型名称及网络"
            if isinstance(exc, AuthenticationFailure):
                message = "认证失败，请检查模型凭据"
            elif isinstance(exc, RateLimitFailure):
                message = "请求受限，请检查服务额度或稍后重试"
            elif isinstance(exc, ContextBudgetError):
                message = "上下文预算配置不可用，请填写真实的上下文窗口（context_window_tokens），并核对输出额度。"
            return {"ok": False, "message": message}
        finally:
            close = getattr(client, "close", None)
            if close:
                await close()

    @app.get("/api/skills")
    async def get_skills() -> dict[str, object]:
        return {"items": (skills_list(workspace.cwd))}

    @app.patch("/api/skills/{skill_id}")
    async def patch_skill(skill_id: str, data: SkillToggle) -> dict[str, object]:
        (toggle_skill(workspace.cwd, skill_id, data.enabled))
        return {"ok": True}

    @app.get("/api/skills/{skill_id}")
    async def get_skill_detail(skill_id: str) -> dict[str, object]:
        return skill_detail(workspace.cwd, skill_id)

    @app.get("/api/sessions")
    async def get_sessions() -> dict[str, object]:
        return {
            "items": Redactor().clean(
                [
                    {k: r[k] for k in ("session_id", "summary", "profile_id", "updated_at")}
                    for r in (await workspace.store.list_snapshots(workspace.cwd))
                ]
            )
        }

    @app.post("/api/sessions", status_code=201)
    async def add_session(data: SessionInput) -> dict[str, object]:
        profile_id = data.profile_id or (models_list())["active_profile"]
        # Allow an unconfigured API profile so first-use UI can guide the user.
        profile_settings(profile_id)
        return dict(session_view((await workspace.store.create(profile_id))))

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, object]:
        return Redactor().clean(dict(session_view((await workspace.record(session_id)))))

    async def session_files(session_id: str) -> SessionFiles:
        (await workspace.record(session_id))
        return SessionFiles(ResearchStore(workspace.cwd, session_id))

    def require_idle_files(session_id: str) -> None:
        connection = workspace.connections.get(session_id) or workspace.active_runs.get(session_id)
        if session_id in workspace.file_operations or (connection and connection.busy):
            raise HTTPException(409, "请等待文件操作或当前生成完成")

    @app.get("/api/sessions/{session_id}/attachments")
    async def list_attachments(session_id: str) -> dict[str, object]:
        return {"items": (await (await session_files(session_id)).list("attachments"))}

    @app.post("/api/sessions/{session_id}/attachments", status_code=201)
    async def upload_attachments(session_id: str, request: Request) -> dict[str, object]:
        storage = await session_files(session_id)
        require_idle_files(session_id)
        # Multipart is bounded both in file count and in actual streamed bytes.
        workspace.file_operations.add(session_id)
        created = []
        try:
            async with request.form(max_files=10, max_fields=0) as form:
                uploads = [
                    item
                    for key, item in form.multi_items()
                    if key == "files" and isinstance(item, UploadFile)
                ]
                if not uploads or len(uploads) != len(form.multi_items()):
                    raise HTTPException(422, "请通过files字段上传1至10个PDF、TXT或MD文件")
                for upload in uploads:
                    content = bytearray()
                    while chunk := await upload.read(1024 * 1024):
                        content.extend(chunk)
                        if len(content) > MAX_DOCUMENT_BYTES:
                            raise HTTPException(413, "每个文件最多30 MB")
                    try:
                        meta = await storage.upload(upload.filename or "", bytes(content))
                    except ValueError as exc:
                        raise HTTPException(422, str(exc)) from None
                    created.append(meta)
            return {"items": created}
        except Exception:
            for meta in created:
                await storage.delete_attachment(meta["id"])
            raise
        finally:
            workspace.file_operations.discard(session_id)

    @(app.delete("/api/sessions/{session_id}/attachments/{file_id}"))
    async def delete_attachment(session_id: str, file_id: str) -> dict[str, object]:
        storage = await session_files(session_id)
        require_idle_files(session_id)
        try:
            await storage.delete_attachment(file_id)
        except (ValueError, FileNotFoundError):
            raise HTTPException(404, "附件不存在") from None
        return {"ok": True}

    @app.get("/api/sessions/{session_id}/artifacts")
    async def list_artifacts(session_id: str) -> dict[str, object]:
        return {"items": (await (await session_files(session_id)).list("artifacts"))}

    @app.get("/api/sessions/{session_id}/deliveries")
    async def list_deliveries(session_id: str) -> dict[str, object]:
        await workspace.record(session_id)
        service = DeliveryService(ResearchStore(workspace.cwd, session_id))
        return {"items": await service.list()}

    @app.post("/api/sessions/{session_id}/deliveries/{delivery_id}/review")
    async def review_delivery(
        session_id: str, delivery_id: str, review: ReviewRequest
    ) -> dict[str, object]:
        await workspace.record(session_id)
        from researchx.web.local_identity import local_reviewer

        service = DeliveryService(ResearchStore(workspace.cwd, session_id))
        try:
            return await service.decide(
                delivery_id,
                review.decision,
                review.note,
                principal=local_reviewer(),
                expected_manifest_hash=review.expected_manifest_hash,
                reviewed_claim_ids=review.reviewed_claim_ids,
            )
        except ResearchError as exc:
            message = str(exc)
            if message == "Unknown delivery":
                status = 404
            elif "Incomplete" in message or "changed" in message or "不能" in message:
                status = 409
            else:
                status = 422
            raise HTTPException(status, message) from None

    @app.get("/api/sessions/{session_id}/artifacts/{file_id}/download")
    async def download_artifact(session_id: str, file_id: str) -> FileResponse:
        try:
            meta, path = await (await session_files(session_id)).artifact(file_id)
        except (ValueError, FileNotFoundError):
            raise HTTPException(404, "产物不存在或不属于当前会话") from None
        if meta.get("kind") == "report_draft":
            store = ResearchStore(workspace.cwd, session_id)
            memory = await store.load()
            artifact = next(
                (item for item in memory.artifacts.values() if item.file_id == file_id), None
            )
            if artifact is None:
                raise HTTPException(403, "报告尚未完成来源授权分类")
            service = DeliveryService(store)
            delivery = next(
                (
                    item
                    for item in await service.list()
                    if item["kind"] == "report_draft" and item["origin_id"] == artifact.id
                ),
                None,
            )
            if delivery is None:
                raise HTTPException(403, "报告尚未冻结为可交付版本")
            try:
                await service.export_text(str(delivery["delivery_id"]))
            except (ResearchError, FileNotFoundError):
                raise HTTPException(403, "来源授权不足，不能下载此报告") from None
            if hashlib.sha256(path.read_bytes()).hexdigest() != delivery["content_hash"]:
                raise HTTPException(409, "报告文件与冻结版本不一致")
        elif meta.get("kind") in {
            "financial",
            "monitor",
            "digest",
            "deep",
            "financial-commentary",
            "industry-commentary",
            "industry-deep-dive",
        }:
            service = DeliveryService(ResearchStore(workspace.cwd, session_id))
            delivery = next(
                (
                    item
                    for item in await service.list()
                    if item["kind"] == "report_file" and item["origin_id"] == file_id
                ),
                None,
            )
            if delivery is None:
                raise HTTPException(403, "历史报告须重新核对来源并冻结版本后下载")
            try:
                body = await service.export_text(str(delivery["delivery_id"]))
            except (ResearchError, FileNotFoundError):
                raise HTTPException(403, "来源授权不足或版本已失效，不能下载此报告") from None
            if hashlib.sha256(body).hexdigest() != hashlib.sha256(path.read_bytes()).hexdigest():
                raise HTTPException(409, "报告文件与冻结版本不一致")
        return FileResponse(path, filename=meta["name"], media_type="application/octet-stream")

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, object]:
        try:
            workspace.store._path(session_id)  # Validate the public identifier.
        except ValueError:
            raise HTTPException(404, "会话不存在") from None
        if await workspace.store.records.load(session_id) is None:
            raise HTTPException(404, "会话不存在")
        connection = workspace.connections.get(session_id) or workspace.active_runs.get(session_id)
        if session_id in workspace.deleting:
            raise HTTPException(409, "会话正在删除")
        if session_id in workspace.file_operations:
            raise HTTPException(409, "请等待文件操作完成，再删除对话")
        if (
            connection
            and (connection.busy or connection.accepting or connection.queued_request is not None)
            or session_id in workspace._queued
        ):
            raise HTTPException(409, "请先停止当前生成，再删除对话")
        workspace.deleting.add(session_id)
        deleted = False
        try:
            # No await between checking for a running task and closing submissions.
            await workspace.store.delete(session_id)
            deleted = True
            if connection:
                await connection.emit("session_deleted")
                await connection.channel.finish()
        except OSError:
            raise HTTPException(500, "删除失败，请检查数据库连接后重试") from None
        finally:
            workspace.deleting.discard(session_id)
            if deleted and connection and workspace.connections.get(session_id) is connection:
                workspace.connections.pop(session_id, None)
        return {"ok": True}

    @app.patch("/api/sessions/{session_id}")
    async def change_session_model(session_id: str, data: SessionInput) -> dict[str, object]:
        connection = workspace.connections.get(session_id)
        if connection and connection.busy:
            raise HTTPException(409, "请先停止当前生成，再切换模型")
        if not data.profile_id:
            raise HTTPException(422, "请选择模型配置")
        profile_settings(data.profile_id)
        record = await workspace.record(session_id)
        record["profile_id"] = data.profile_id
        (await workspace.store.write(record))
        return Redactor().clean(dict(session_view(record)))

    @app.get("/api/sessions/{session_id}/events")
    async def events(
        session_id: str,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        record = await workspace.record(session_id)
        if session_id in workspace.connections:
            raise HTTPException(409, "此会话已在另一个窗口打开")
        cursor = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0

        def event_cursor(item: dict[str, object]) -> int:
            raw = item.get("event_id", 0)
            return int(raw) if isinstance(raw, (int, str)) and not isinstance(raw, bool) else 0

        live = workspace.active_runs.get(session_id)
        if live is not None and (live.busy or live.queued_request is not None or live.accepting):
            async with live.send_lock:
                # Freeze the replay boundary while redirecting new events to
                # the fresh channel. An event belongs to exactly one side.
                live.channel = EventChannel()
                replay = [item for item in live.event_log if event_cursor(item) > cursor]
            connection = live
        else:
            connection = SessionController(session_id, workspace)
            replay = []
        workspace.connections[session_id] = connection

        async def close() -> None:
            connection.channel.detach()
            with CancelScope(shield=True):
                try:
                    await connection.unsubscribe()
                finally:
                    if workspace.connections.get(session_id) is connection:
                        workspace.connections.pop(session_id, None)

        async def stream() -> AsyncIterator[str]:
            try:
                # The browser needs the active request ID before it can accept
                # replayed run events. Ready is connection-local, not logged.
                connection.ready = True
                yield encode_event(
                    connection.redactor.clean(
                        {
                            "type": "ready",
                            "session_id": session_id,
                            "request_id": connection.request_id,
                            "connection_id": connection.connection_id,
                            "session": session_view(record),
                            "running": connection.busy
                            or connection.queued_request is not None
                            or connection.accepting,
                        }
                    )
                )
                for item in replay:
                    yield encode_event(item)
                async for frame in connection.channel.stream():
                    yield frame
            finally:
                # Starlette cancels the iterator on disconnect. Shield persistence
                # from that cancellation scope while awaiting the owned tasks.
                await close()

        return SessionEventResponse(stream(), close)

    @app.post("/api/sessions/{session_id}/commands", status_code=202)
    async def commands(
        session_id: str,
        command: CommandRequest,
        connection_id: str | None = Header(default=None, alias="X-ResearchX-Connection"),
    ) -> dict[str, object]:
        (await workspace.record(session_id))
        connection = workspace.connections.get(session_id) or workspace.active_runs.get(session_id)
        if connection is None:
            if command.type not in {"submit", "resume"}:
                raise HTTPException(409, "没有可控制的研究运行")
            connection = SessionController(session_id, workspace)
            connection.ready = True
        elif (
            connection_id
            and connection_id != connection.connection_id
            and session_id in workspace.connections
        ):
            raise HTTPException(409, "命令不属于当前会话控制连接")
        return await connection.command(command)

    if static_dir is None:
        packaged = Path(__file__).parents[1] / "_web"
        source = Path(__file__).parents[3] / "frontend" / "web" / "dist"
        static_dir = packaged if packaged.is_dir() else source
    if (static_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    @app.get("/{path:path}")
    async def frontend(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(404, "接口不存在")
        if path in {"", "chat", "models", "skills"} and (static_dir / "index.html").is_file():
            return FileResponse(static_dir / "index.html")
        raise HTTPException(404, "请先构建前端：cd frontend/web && npm ci && npm run build")

    return app
