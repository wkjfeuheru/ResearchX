"""Owned Docker sessions. A context-local legacy selection never crosses queries."""

from __future__ import annotations

import atexit
from contextvars import ContextVar
from pathlib import Path

from researchx.config import Settings
from researchx.sandbox.docker_backend import DockerSandboxSession, get_docker_availability
from researchx.sandbox.adapter import SandboxUnavailableError

_sessions: dict[str, DockerSandboxSession] = {}
_current_owner: ContextVar[str | None] = ContextVar("sandbox_owner", default=None)


def get_docker_sandbox(owner: str | None = None) -> DockerSandboxSession | None:
    key = owner or _current_owner.get()
    return _sessions.get(key) if key else None


def is_docker_sandbox_active() -> bool:
    session = get_docker_sandbox()
    return session is not None and session.is_running


async def start_docker_sandbox(
    settings: Settings, session_id: str, cwd: Path, *, report: bool = False
) -> DockerSandboxSession | None:
    availability = get_docker_availability(settings)
    if not availability.available:
        if settings.sandbox.fail_if_unavailable:
            raise SandboxUnavailableError(availability.reason or "Docker sandbox is unavailable")
        return None
    session = DockerSandboxSession(settings=settings, session_id=session_id, cwd=cwd, report=report)
    # Register before start: close/cancel can also clean up a partially started container.
    _sessions[session_id] = session
    try:
        await session.start()
    except BaseException as exc:
        try:
            await stop_docker_sandbox(session_id)
        except BaseException as cleanup_error:
            setattr(exc, "cleanup_errors", [cleanup_error])
        raise
    _current_owner.set(session_id)
    atexit.register(session.stop_sync)
    return session


async def stop_docker_sandbox(owner: str | None = None) -> None:
    key = owner or _current_owner.get()
    session = _sessions.get(key) if key else None
    if session is not None:
        assert key is not None
        await session.stop()
        _sessions.pop(key, None)
        atexit.unregister(session.stop_sync)
    if key == _current_owner.get():
        _current_owner.set(None)


async def stop_runtime_sandboxes(runtime_id: str) -> None:
    errors = []
    for key in list(_sessions):
        if key == runtime_id or key.startswith(runtime_id + ":"):
            try:
                await stop_docker_sandbox(key)
            except Exception as exc:
                errors.append(exc)
    if errors:
        raise RuntimeError("Sandbox cleanup failed") from errors[0]
