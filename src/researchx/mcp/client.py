"""MCP client manager."""

from __future__ import annotations

from pydantic import AnyUrl
from researchx.services.execution.async_timeout import timeout as async_timeout
import asyncio
from contextlib import AsyncExitStack
from typing import TypeVar, Coroutine, Any
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, ReadResourceResult
from researchx.mcp.types import (
    McpServerConfig,
    McpConnectionStatus,
    McpHttpServerConfig,
    McpResourceInfo,
    McpStdioServerConfig,
    McpToolInfo,
)


RequestT = TypeVar("RequestT")


class McpServerNotConnectedError(Exception):
    """Raised when a session is absent or lost. Only pre-dispatch absence proves no effect."""

    def __init__(self, message: str, *, request_not_sent: bool = False) -> None:
        super().__init__(message)
        self.request_not_sent = request_not_sent


class McpToolReturnedError(Exception):
    """The connected MCP server failed to return a usable tool result."""

    def __init__(self, message: str, *, code: str = "tool_error") -> None:
        super().__init__(message)
        self.code = code


class McpClientManager:
    """Manage MCP connections and expose tools/resources."""

    def __init__(self, server_configs: dict[str, McpServerConfig]) -> None:
        self._server_configs = server_configs
        self._statuses: dict[str, McpConnectionStatus] = {
            name: McpConnectionStatus(
                name=name,
                state="pending",
                transport=getattr(config, "type", "unknown"),
            )
            for name, config in server_configs.items()
        }
        self._sessions: dict[str, ClientSession] = {}
        self._stacks: dict[str, AsyncExitStack] = {}
        self._owners: dict[str, asyncio.Task[None]] = {}
        self._shutdown: dict[str, asyncio.Event] = {}
        self._cleanup_errors: list[BaseException] = []
        self._close_lock = asyncio.Lock()

    async def connect_all(self) -> None:
        """Connect all configured MCP servers supported by the current build."""
        for name, config in self._server_configs.items():
            if name in self._owners:
                continue
            ready = asyncio.Event()
            self._shutdown[name] = asyncio.Event()
            self._owners[name] = asyncio.create_task(self._own_connection(name, config, ready))
            try:
                await ready.wait()
            except asyncio.CancelledError:
                self._owners[name].cancel()
                await self.close()
                raise

    async def _own_connection(
        self, name: str, config: McpServerConfig, ready: asyncio.Event
    ) -> None:
        """Keep transport cancel scopes off the caller's research task."""
        try:
            if isinstance(config, McpStdioServerConfig):
                await self._connect_stdio(name, config)
            elif isinstance(config, McpHttpServerConfig):
                await self._connect_http(name, config)
            else:
                self._statuses[name] = McpConnectionStatus(
                    name=name,
                    state="failed",
                    transport=config.type,
                    auth_configured=bool(getattr(config, "headers", None)),
                    detail=f"Unsupported MCP transport in current build: {config.type}",
                )
            ready.set()
            if name in self._sessions:
                await self._shutdown[name].wait()
        except asyncio.CancelledError:
            # Transport TaskGroups cancel their owner on HTTP/stream failure.
            # Only this task owns those scopes; the research caller stays alive.
            pass
        except Exception:
            pass
        finally:
            stack = self._stacks.pop(name, None)
            if stack is not None:
                await self._close_failed_stack(stack)
            self._sessions.pop(name, None)
            if not self._shutdown[name].is_set() and self._statuses[name].state != "failed":
                self._statuses[name].state = "failed"
                self._statuses[name].detail = "MCP transport closed during a request"
            ready.set()

    async def reconnect_all(self) -> None:
        """Reconnect all configured servers."""
        await self.close()
        self._statuses = {
            name: McpConnectionStatus(
                name=name, state="pending", transport=getattr(config, "type", "unknown")
            )
            for name, config in self._server_configs.items()
        }
        await self.connect_all()

    def update_server_config(self, name: str, config: McpServerConfig) -> None:
        """Replace one server config in memory."""
        self._server_configs[name] = config

    def get_server_config(self, name: str) -> McpServerConfig | None:
        """Return one configured server object if present."""
        return self._server_configs.get(name)

    async def _close_failed_stack(self, stack: AsyncExitStack) -> None:
        """Best-effort cleanup for a connection attempt that never finished."""
        try:
            await stack.aclose()
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            self._cleanup_errors.append(exc)

    def _mark_connection_failed(
        self,
        name: str,
        config: McpServerConfig,
        *,
        auth_configured: bool,
        exc: BaseException,
    ) -> None:
        """Record one MCP connection failure without aborting startup."""
        self._statuses[name] = McpConnectionStatus(
            name=name,
            state="failed",
            transport=getattr(config, "type", "unknown"),
            auth_configured=auth_configured,
            detail=str(exc) or exc.__class__.__name__,
        )

    async def close(self) -> None:
        """Close all active MCP sessions."""
        async with self._close_lock:
            for signal in self._shutdown.values():
                signal.set()
            owners = set(self._owners.values())
            if owners:
                _, pending = await asyncio.wait(owners, timeout=10)
                for owner in pending:
                    owner.cancel()
                if pending:
                    _, pending = await asyncio.wait(pending, timeout=3)
                if pending:
                    raise RuntimeError("MCP connection owners did not stop")
                for owner in owners:
                    error = owner.exception() if not owner.cancelled() else None
                    if error is not None:
                        self._cleanup_errors.append(error)
            self._owners.clear()
            self._shutdown.clear()
            for stack in reversed(list(self._stacks.values())):
                await self._close_failed_stack(stack)
            self._stacks.clear()
            self._sessions.clear()
            if self._cleanup_errors:
                errors, self._cleanup_errors = self._cleanup_errors, []
                raise RuntimeError("MCP cleanup failed") from errors[0]

    def list_statuses(self) -> list[McpConnectionStatus]:
        """Return statuses for all configured servers."""
        return [self._statuses[name] for name in sorted(self._statuses)]

    def list_tools(self) -> list[McpToolInfo]:
        """Return all connected MCP tools."""
        tools: list[McpToolInfo] = []
        for status in self.list_statuses():
            tools.extend(status.tools)
        return tools

    def list_resources(self) -> list[McpResourceInfo]:
        """Return all connected MCP resources."""
        resources: list[McpResourceInfo] = []
        for status in self.list_statuses():
            resources.extend(status.resources)
        return resources

    async def _request(
        self, server_name: str, awaitable: Coroutine[object, object, RequestT]
    ) -> RequestT:
        owner = self._owners.get(server_name)
        if owner is None:
            return await awaitable
        request = asyncio.create_task(awaitable)
        try:
            done, _ = await asyncio.wait({request, owner}, return_when=asyncio.FIRST_COMPLETED)
            if request in done:
                return await request
            raise McpServerNotConnectedError(
                f"MCP server '{server_name}' transport closed during the request"
            )
        finally:
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> str:
        """Invoke one MCP tool and stringify the result."""
        session = self._sessions.get(server_name)
        if session is None:
            status = self._statuses.get(server_name)
            detail = status.detail if status else "unknown server"
            raise McpServerNotConnectedError(
                f"MCP server '{server_name}' is not connected: {detail}",
                request_not_sent=True,
            )
        timeout = getattr(self._server_configs.get(server_name), "request_timeout", 60.0)
        try:
            async with async_timeout(timeout):
                result: CallToolResult = await self._request(
                    server_name, session.call_tool(tool_name, arguments)
                )
        except asyncio.TimeoutError as exc:
            raise McpToolReturnedError(
                f"MCP tool '{server_name}/{tool_name}' timed out after {timeout:g}s. "
                "No result was received; do not treat this as evidence or retry unchanged.",
                code="timeout",
            ) from exc
        except RuntimeError as exc:
            # The MCP SDK raises RuntimeError for output-schema validation failures.
            # Keep validation enabled and do not coerce missing financial data.
            if not (
                str(exc).startswith(
                    ("Invalid structured content", "Invalid schema for tool", "Unresolvable")
                )
                or "has an output schema but did not return structured content" in str(exc)
            ):
                raise McpServerNotConnectedError(
                    f"MCP server '{server_name}' call failed: transport closed or unavailable"
                ) from exc
            raise McpToolReturnedError(
                f"MCP tool '{server_name}/{tool_name}' returned an invalid response "
                "or failed during result processing. No usable evidence was received.",
                code="invalid_response",
            ) from exc
        except Exception as exc:
            raise McpServerNotConnectedError(
                f"MCP server '{server_name}' call failed: {type(exc).__name__}"
            ) from exc
        parts: list[str] = []
        for item in result.content:
            if getattr(item, "type", None) == "text":
                parts.append(getattr(item, "text", ""))
            else:
                parts.append(item.model_dump_json())
        if result.structuredContent and not parts:
            parts.append(str(result.structuredContent))
        if not parts:
            parts.append("(no output)")
        output = "\n".join(parts).strip()
        if result.isError is True:
            raise McpToolReturnedError(output)
        return output

    async def read_resource(self, server_name: str, uri: str) -> str:
        """Read one MCP resource and stringify the response."""
        session = self._sessions.get(server_name)
        if session is None:
            status = self._statuses.get(server_name)
            detail = status.detail if status else "unknown server"
            raise McpServerNotConnectedError(
                f"MCP server '{server_name}' is not connected: {detail}",
                request_not_sent=True,
            )
        timeout = getattr(self._server_configs.get(server_name), "request_timeout", 60.0)
        try:
            async with async_timeout(timeout):
                result: ReadResourceResult = await self._request(
                    server_name, session.read_resource(AnyUrl(uri))
                )
        except asyncio.TimeoutError as exc:
            raise McpServerNotConnectedError(
                f"MCP resource read from '{server_name}' timed out after {timeout:g}s. No result was received."
            ) from exc
        except Exception as exc:
            raise McpServerNotConnectedError(
                f"MCP server '{server_name}' resource read failed: {exc}"
            ) from exc
        parts: list[str] = []
        for item in result.contents:
            text = getattr(item, "text", None)
            if text is not None:
                parts.append(text)
            else:
                parts.append(str(getattr(item, "blob", "")))
        return "\n".join(parts).strip()

    async def _connect_stdio(self, name: str, config: McpStdioServerConfig) -> None:
        stack = AsyncExitStack()
        try:
            read_stream, write_stream = await stack.enter_async_context(
                stdio_client(
                    StdioServerParameters(
                        command=config.command,
                        args=config.args,
                        env=config.env,
                        cwd=config.cwd,
                    )
                )
            )
            await self._register_connected_session(
                name=name,
                config=config,
                stack=stack,
                read_stream=read_stream,
                write_stream=write_stream,
                auth_configured=bool(config.env),
            )
        except asyncio.CancelledError as exc:
            await self._close_failed_stack(stack)
            self._mark_connection_failed(
                name,
                config,
                auth_configured=bool(config.env),
                exc=exc,
            )
        except Exception as exc:
            await self._close_failed_stack(stack)
            self._mark_connection_failed(
                name,
                config,
                auth_configured=bool(config.env),
                exc=exc,
            )

    async def _connect_http(self, name: str, config: McpHttpServerConfig) -> None:
        stack = AsyncExitStack()
        try:
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(headers=config.headers or None)
            )
            read_stream, write_stream, _get_session_id = await stack.enter_async_context(
                streamable_http_client(config.url, http_client=http_client)
            )
            await self._register_connected_session(
                name=name,
                config=config,
                stack=stack,
                read_stream=read_stream,
                write_stream=write_stream,
                auth_configured=bool(config.headers),
            )
        except asyncio.CancelledError as exc:
            await self._close_failed_stack(stack)
            self._mark_connection_failed(
                name,
                config,
                auth_configured=bool(config.headers),
                exc=exc,
            )
        except Exception as exc:
            await self._close_failed_stack(stack)
            self._mark_connection_failed(
                name,
                config,
                auth_configured=bool(config.headers),
                exc=exc,
            )

    async def _register_connected_session(
        self,
        *,
        name: str,
        config: McpServerConfig,
        stack: AsyncExitStack,
        read_stream: Any,
        write_stream: Any,
        auth_configured: bool,
    ) -> None:
        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        tool_result = await session.list_tools()
        resource_result = None
        try:
            resource_result = await session.list_resources()
        except Exception as exc:
            if "Method not found" not in str(exc):
                raise
        tools = [
            McpToolInfo(
                server_name=name,
                name=tool.name,
                description=tool.description or "",
                input_schema=dict(tool.inputSchema or {"type": "object", "properties": {}}),
            )
            for tool in tool_result.tools
        ]
        resources = [
            McpResourceInfo(
                server_name=name,
                name=resource.name or str(resource.uri),
                uri=str(resource.uri),
                description=resource.description or "",
            )
            for resource in (resource_result.resources if resource_result is not None else [])
        ]
        self._sessions[name] = session
        self._stacks[name] = stack
        self._statuses[name] = McpConnectionStatus(
            name=name,
            state="connected",
            transport=getattr(config, "type", "unknown"),
            auth_configured=auth_configured,
            tools=tools,
            resources=resources,
        )
