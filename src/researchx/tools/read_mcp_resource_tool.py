"""Tool to read MCP resources."""

from __future__ import annotations

from pydantic import BaseModel, Field

from researchx.mcp.client import McpClientManager, McpServerNotConnectedError
from researchx.tools.base import BaseTool, ToolExecutionContext, ToolResult


class ReadMcpResourceToolInput(BaseModel):
    """Arguments for reading an MCP resource."""

    server: str = Field(description="MCP 服务器名称")
    uri: str = Field(description="资源 URI")


class ReadMcpResourceTool(BaseTool[ReadMcpResourceToolInput]):
    """Read one resource from an MCP server."""

    name = "read_mcp_resource"
    description = "根据服务器和 URI 读取 MCP 资源。"
    input_model = ReadMcpResourceToolInput

    def __init__(self, manager: McpClientManager) -> None:
        self._manager = manager

    def execution_contract(self, arguments: ReadMcpResourceToolInput | None = None) -> object:
        if arguments is None:
            return None
        # The native resource-read protocol, unlike arbitrary MCP tool annotations.
        return {
            "name": self.name,
            "source": "builtin",
            "effect": "read_only",
            "required_capabilities": ("mcp.call",),
            "resources_read": (f"mcp:{arguments.server}",),
            "parallelism": "resources",
        }

    def is_read_only(self, arguments: ReadMcpResourceToolInput) -> bool:
        del arguments
        return True

    async def execute(
        self, arguments: ReadMcpResourceToolInput, context: ToolExecutionContext
    ) -> ToolResult:
        del context
        try:
            output = await self._manager.read_resource(arguments.server, arguments.uri)
        except McpServerNotConnectedError as exc:
            return ToolResult(output=str(exc), is_error=True)
        return ToolResult(
            output=output,
            metadata={
                "research_source_specs": [
                    {
                        "kind": "mcp",
                        "title": arguments.uri,
                        "locator": f"mcp:{arguments.server}/{arguments.uri}",
                        "content": output,
                        "fragment": False,
                    }
                ]
            },
        )
