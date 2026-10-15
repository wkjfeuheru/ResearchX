"""MCP tool adapters."""

from __future__ import annotations

import re
import json
from copy import deepcopy
from typing import Any, Protocol

from pydantic import RootModel
from jsonschema.validators import validator_for  # type: ignore[import-untyped]
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from referencing.exceptions import Unresolvable
from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]

from researchx.mcp.client import McpServerNotConnectedError, McpToolReturnedError
from researchx.mcp.types import McpToolInfo
from researchx.tools.base import BaseTool, ToolExecutionContext, ToolResult


class McpToolCaller(Protocol):
    async def call_tool(
        self, server_name: str, tool_name: str, arguments: dict[str, Any]
    ) -> str: ...


class McpSchemaError(ValueError):
    pass


class McpArgumentError(ValueError):
    pass


class McpArguments(RootModel[dict[str, Any]]):
    pass


class McpToolAdapter(BaseTool[McpArguments]):
    """Expose one MCP tool as a normal ResearchX tool."""

    def __init__(
        self, manager: McpToolCaller, tool_info: McpToolInfo, *, registry: Registry | None = None
    ) -> None:
        self._manager = manager
        self._tool_info = tool_info
        server_segment = _sanitize_tool_segment(tool_info.server_name)
        tool_segment = _sanitize_tool_segment(tool_info.name)
        self.name = f"mcp__{server_segment}__{tool_segment}"
        self.contract = {
            "name": self.name,
            "source": "mcp",
            "effect": "external_write",
            "required_capabilities": ("mcp.call",),
            "resources_write": (f"mcp:{tool_info.server_name}",),
            "parallelism": "resources",
        }
        self.description = tool_info.description or f"MCP 工具 {tool_info.name}"
        self.input_model = McpArguments
        registry = Registry().with_resources(
            (
                uri,
                Resource.from_contents(
                    deepcopy(resource.contents), default_specification=resource._specification
                ),
            )
            for uri, resource in (registry.items() if registry is not None else ())
        )
        self._original_schema = deepcopy(tool_info.input_schema)
        schema = self._original_schema
        validator_class = (
            validator_for(schema, default=None) if "$schema" in schema else validator_for(schema)
        )
        if validator_class is None:
            raise McpSchemaError(f"{self.name}: unsupported JSON Schema dialect")
        try:
            validator_class.check_schema(schema)
            resource = Resource.from_contents(schema, default_specification=DRAFT202012)
            resolver = registry.resolver_with_root(resource)
            self._check_schema_resources(resource, resolver, validator_class.FORMAT_CHECKER, set())
        except (SchemaError, Unresolvable, ValueError):
            raise McpSchemaError(
                f"{self.name}: invalid or unsupported JSON Schema, format or reference"
            ) from None
        self._validator = validator_class(
            schema,
            registry=registry,
            format_checker=validator_class.FORMAT_CHECKER,
        )

    def _check_schema_resources(
        self, resource: Any, resolver: Any, checker: Any, seen: set[int]
    ) -> None:
        if id(resource.contents) in seen:
            return
        seen.add(id(resource.contents))
        schema = resource.contents
        if isinstance(schema, dict):
            if "$schema" in schema and validator_for(schema, default=None) is None:
                raise ValueError("unsupported dialect")
            if "format" in schema and schema["format"] not in checker.checkers:
                raise ValueError("unsupported format")
            supported = {
                "https://json-schema.org/draft/2020-12/vocab/" + name
                for name in (
                    "core",
                    "applicator",
                    "unevaluated",
                    "validation",
                    "meta-data",
                    "format-annotation",
                    "format-assertion",
                    "content",
                )
            } | {
                "https://json-schema.org/draft/2019-09/vocab/" + name
                for name in ("core", "applicator", "validation", "meta-data", "format", "content")
            }
            if any(
                required and uri not in supported
                for uri, required in schema.get("$vocabulary", {}).items()
            ):
                raise ValueError("unsupported required vocabulary")
            for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
                if keyword in schema:
                    resolved = resolver.lookup(schema[keyword])
                    validator_for(
                        resolved.contents, default=validator_for(self._original_schema)
                    ).check_schema(resolved.contents)
                    target = Resource.from_contents(
                        resolved.contents, default_specification=resource._specification
                    )
                    self._check_schema_resources(target, resolved.resolver, checker, seen)
        for child in resource.subresources():
            self._check_schema_resources(child, resolver.in_subresource(child), checker, seen)

    def input_schema(self) -> dict[str, Any]:
        return deepcopy(self._original_schema)

    def parse_input(self, payload: dict[str, Any]) -> McpArguments:
        raw = deepcopy(payload)
        if not isinstance(raw, dict):
            raise McpArgumentError("/: object required")
        try:
            if json.loads(json.dumps(raw, allow_nan=False)) != raw:
                raise ValueError("Non-JSON argument")
            error = next(self._validator.iter_errors(raw), None)
        except Exception:
            raise McpArgumentError("/: schema reference or validation unavailable") from None
        if error is not None:
            path = "/" + "/".join(str(part) for part in error.absolute_path)
            raise McpArgumentError(f"{path}: {error.validator}") from None
        return McpArguments(root=raw)

    def validation_error_message(self, error: Exception) -> str:
        detail = str(error) if isinstance(error, McpArgumentError) else "/: invalid arguments"
        return f"Invalid input for {self.name}: {detail}"

    async def execute(self, arguments: McpArguments, context: ToolExecutionContext) -> ToolResult:
        del context
        raw = self.parse_input(arguments.root).root
        try:
            output = await self._manager.call_tool(
                self._tool_info.server_name,
                self._tool_info.name,
                raw,
            )
        except (McpServerNotConnectedError, McpToolReturnedError) as exc:
            code = getattr(exc, "code", "connection")
            description = {
                "invalid_response": "工具返回异常：数据结构或结果处理失败",
                "timeout": "工具执行超时",
                "connection": "外部服务连接失败",
                "tool_error": "外部服务返回工具错误",
            }.get(code, "外部工具失败")
            detail = f"{self._tool_info.server_name}/{self._tool_info.name}：{description}"
            return ToolResult(
                output=str(exc),
                is_error=True,
                metadata={
                    "outcome": "error",
                    "error_code": code,
                    "detail": detail,
                    "research_source_specs": [],
                    "no_effect": isinstance(exc, McpServerNotConnectedError)
                    and exc.request_not_sent,
                },
            )
        return ToolResult(
            output=output,
            metadata={
                "research_source_specs": [
                    {
                        "kind": "mcp",
                        "title": self._tool_info.name,
                        "locator": f"mcp:{self._tool_info.server_name}/{self._tool_info.name}",
                        "content": output,
                        "fragment": False,
                    }
                ]
            },
        )


def _sanitize_tool_segment(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "_", value)
    if not sanitized:
        return "tool"
    if not sanitized[0].isalpha():
        return f"mcp_{sanitized}"
    return sanitized
