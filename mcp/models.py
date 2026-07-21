"""Data models for NOVA's MCP integration layer."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class TransportType(str, Enum):
    STDIO = "stdio"
    SSE = "sse"


@dataclass
class ServerConfig:
    """Config for one MCP server NOVA can connect to."""
    name: str                      # unique id, e.g. "filesystem", "github"
    transport: TransportType
    command: Optional[str] = None       # for stdio: executable
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: Optional[str] = None           # for sse
    auth_token: Optional[str] = None
    enabled: bool = True
    timeout_s: float = 15.0


@dataclass
class ToolSpec:
    """A discovered tool, namespaced by its owning server."""
    server_name: str
    name: str                      # raw tool name from the server
    description: str
    input_schema: dict[str, Any]

    @property
    def qualified_name(self) -> str:
        # avoids collisions when two servers expose a tool called "search"
        return f"{self.server_name}:{self.name}"


class MCPError(Exception):
    """Base error for MCP layer failures."""


class MCPConnectionError(MCPError):
    pass


class MCPToolCallError(MCPError):
    def __init__(self, tool: str, detail: str):
        self.tool = tool
        self.detail = detail
        super().__init__(f"tool '{tool}' failed: {detail}")