"""MCP contracts preserve validated JSON and reject invalid input before dispatch."""

from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from researchx.mcp.types import McpToolInfo
from researchx.tools.base import ToolExecutionContext, ToolRegistry
from researchx.tools.contracts import resolve_contract
from researchx.tools.mcp_tool import McpArgumentError, McpArguments, McpToolAdapter


def adapter(schema, **kwargs):
    manager = AsyncMock()
    manager.call_tool.return_value = "ok"
    tool = McpToolAdapter(manager, McpToolInfo(server_name="fixture", name="lookup", description="", input_schema=schema), **kwargs)
    return tool, manager


SCHEMA = {
    "type": "object",
    "$defs": {"item": {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 3}}, "required": ["n"], "additionalProperties": False}},
    "properties": {
        "kind": {"enum": ["a", "b"]},
        "items": {"type": "array", "minItems": 1, "maxItems": 2, "uniqueItems": True, "items": {"$ref": "#/$defs/item"}},
        "clear": {"type": ["string", "null"]},
        "choice": {"oneOf": [{"type": "integer"}, {"type": "string", "pattern": "^yes$"}]},
    },
    "required": ["kind", "items"],
    "additionalProperties": False,
}


@pytest.mark.parametrize("payload", [
    {"kind": "secret-test-value", "items": [{"n": 1}]},
    {"kind": "a", "items": []},
    {"kind": "a", "items": [{"n": "2"}]},
    {"kind": "a", "items": [{"n": True}]},
    {"kind": "a", "items": [{"n": 4}]},
    {"kind": "a", "items": [{}]},
    {"kind": "a", "items": [{"n": 1}, {"n": 1}]},
    {"kind": "a", "items": [{"n": 1}], "extra": "secret-test-value"},
    {"kind": "a", "items": [{"n": 1}], "choice": False},
])
async def test_invalid_arguments_never_reach_remote(payload):
    tool, manager = adapter(SCHEMA)
    with pytest.raises(McpArgumentError) as error:
        tool.parse_input(payload)
    assert "secret-test-value" not in str(error.value)
    with pytest.raises(McpArgumentError):
        await tool.execute(McpArguments(root=payload), ToolExecutionContext(cwd=Path.cwd()))
    manager.call_tool.assert_not_called()


@pytest.mark.parametrize("extra", [{}, {"clear": None}, {"clear": ""}, {"choice": "yes"}])
async def test_valid_arguments_and_explicit_null_are_preserved(extra):
    schema = deepcopy(SCHEMA)
    tool, manager = adapter(schema)
    payload = {"kind": "a", "items": [{"n": 2}], **extra}
    parsed = tool.parse_input(payload)
    schema["properties"].clear()
    registry = ToolRegistry()
    registry.register(tool)
    expected = deepcopy(SCHEMA)
    expected["required"] = sorted(expected["required"])
    assert registry.to_api_schema()[0]["input_schema"] == expected
    assert resolve_contract(tool).input_schema == SCHEMA
    await tool.execute(parsed, ToolExecutionContext(cwd=Path.cwd()))
    assert manager.call_tool.call_args.args[2] == payload


@pytest.mark.parametrize("schema", [
    {"$schema": "https://unknown.invalid/schema", "type": "object"},
    {"type": "invalid"},
    {"type": "object", "properties": {"secret": {"$ref": "https://remote.invalid/schema"}}},
    {"type": "object", "properties": {"d": {"format": "unimplemented"}}},
    {"$vocabulary": {"https://unknown.invalid/vocab": True}},
])
def test_unsupported_schema_rejected_at_registration(schema):
    with pytest.raises(ValueError, match="Schema|dialect"):
        adapter(schema)


def test_only_host_registered_external_definitions_are_allowed():
    resource = Resource.from_contents({"type": "integer", "minimum": 2}, default_specification=DRAFT202012)
    registry = Registry().with_resource("https://host.invalid/count", resource)
    tool, _ = adapter({"type": "object", "properties": {"n": {"$ref": "https://host.invalid/count"}}}, registry=registry)
    assert tool.parse_input({"n": 2}).root == {"n": 2}
    with pytest.raises(McpArgumentError):
        tool.parse_input({"n": 1})


def test_formats_are_asserted_and_defaults_are_not_added():
    tool, _ = adapter({"type": "object", "properties": {"date": {"type": "string", "format": "date"}, "count": {"type": "integer", "default": 10}}})
    assert tool.parse_input({}).root == {}
    with pytest.raises(McpArgumentError):
        tool.parse_input({"date": "not-a-date"})


@pytest.mark.parametrize("payload", [{"value": float("nan")}, {"value": float("inf")}, {"value": (1, 2)}, {1: "secret"}])
def test_non_json_values_are_rejected_without_coercion(payload):
    tool, manager = adapter({"type":"object"})
    with pytest.raises(McpArgumentError):
        tool.parse_input(payload)
    manager.call_tool.assert_not_called()


async def test_provider_schema_rejection_diagnoses_unavailable_tool_without_conversion(tmp_path):
    from researchx.config.settings import PermissionSettings
    from researchx.engine.messages import ConversationMessage
    from researchx.engine.query import QueryContext, run_query
    from researchx.permissions.checker import PermissionChecker

    tool, manager = adapter({"type":"object", "properties":{"choice":{"oneOf":[{"type":"integer"},{"type":"string"}]}}})
    registry = ToolRegistry()
    registry.register(tool)

    class Client:
        async def stream_message(self, request):
            assert request.tools[0]["input_schema"] == tool.input_schema()
            raise ValueError("Invalid schema for function: unsupported oneOf; secret-test-value")
            yield

    context = QueryContext(api_client=Client(), tool_registry=registry, permission_checker=PermissionChecker(PermissionSettings()),
        cwd=tmp_path, model="test", system_prompt="test", max_tokens=100, context_window_tokens=200000, research_memory_enabled=False)
    events = [event async for event, _ in run_query(context, [ConversationMessage.from_user_text("run")])]
    assert "MCP 工具不可用" in events[-1].message
    assert "secret-test-value" not in events[-1].message
    manager.call_tool.assert_not_called()


def test_host_definitions_are_frozen_and_host_retrieval_is_not_used():
    definition = {"type":"integer", "minimum":2}
    resource = Resource.from_contents(definition, default_specification=DRAFT202012)
    registry = Registry().with_resource("https://host.invalid/count", resource)
    tool, _ = adapter({"type":"object", "properties":{"n":{"$ref":"https://host.invalid/count"}}}, registry=registry)
    definition["minimum"] = 10
    assert tool.parse_input({"n":2}).root == {"n":2}
    requests = []

    def retrieve(uri):
        requests.append(uri)
        return resource

    with pytest.raises(ValueError):
        adapter({"type":"object", "properties":{"n":{"$ref":"https://remote.invalid/count"}}}, registry=Registry(retrieve=retrieve))
    assert not requests


def test_invalid_host_registered_schema_is_rejected_before_execution():
    resource = Resource.from_contents({"type":"invalid"}, default_specification=DRAFT202012)
    registry = Registry().with_resource("https://host.invalid/schema", resource)
    with pytest.raises(ValueError, match="invalid or unsupported JSON Schema"):
        adapter({"type":"object", "properties":{"n":{"$ref":"https://host.invalid/schema"}}}, registry=registry)


@pytest.mark.parametrize("value", [b"secret", (1, 2), float("nan")])
async def test_direct_execution_rejects_non_json_values_before_serialization(value):
    tool, manager = adapter({"type":"object"})
    with pytest.raises(McpArgumentError):
        await tool.execute(McpArguments(root={"value":value}), ToolExecutionContext(cwd=Path.cwd()))
    manager.call_tool.assert_not_called()
