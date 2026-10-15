"""Load independently configured MCP servers."""

from researchx.config import Settings
from researchx.mcp.types import McpServerConfig


def load_mcp_server_configs(settings: Settings) -> dict[str, McpServerConfig]:
    return dict(settings.mcp_servers)
