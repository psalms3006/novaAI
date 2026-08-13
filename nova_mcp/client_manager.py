"""
MCPClientManager: owns lifecycle of connections to one or more MCP servers.

Design decisions (flagging trade-offs, not hiding them):

- One manager instance, many servers. Each server gets its own asyncio task
  running the session; a dead session doesn't take down the others.
  [Alternative rejected: one manager per server -> more moving parts in
  nova_state.py for no real benefit at this scale.]

- Tool discovery result is cached in-memory (registry.py) and only
  re-fetched on connect or explicit refresh(). MCP servers rarely change
  their tool list mid-session; polling on every call wastes latency you
  can't afford in a voice pipeline.

- Failures are isolated per-server. A server that fails to connect is
  logged and marked unavailable; it does not raise out of connect_all(),
  because one broken server config should never block NOVA's boot.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Any, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client

from .models import (
    MCPConnectionError,
    MCPToolCallError,
    ServerConfig,
    ToolSpec,
    TransportType,
)

logger = logging.getLogger("nova.mcp")


class _ServerHandle:
    """Holds one live session plus the exit stack that owns its resources."""

    def __init__(self, config: ServerConfig):
        self.config = config
        self.session: Optional[ClientSession] = None
        self.stack = AsyncExitStack()
        self.tools: dict[str, ToolSpec] = {}
        self.connected = False


class MCPClientManager:
    def __init__(self, configs: Optional[list[ServerConfig]] = None):
        self._servers: dict[str, _ServerHandle] = {}
        for cfg in configs or []:
            self._servers[cfg.name] = _ServerHandle(cfg)
        self._lock = asyncio.Lock()

    # ---------- registration (hot-plug support) ----------

    def register_server(self, config: ServerConfig) -> None:
        """Add a server config without connecting. Call connect(name) after."""
        if config.name in self._servers:
            raise ValueError(f"server '{config.name}' already registered")
        self._servers[config.name] = _ServerHandle(config)

    async def unregister_server(self, name: str) -> None:
        await self.disconnect(name)
        self._servers.pop(name, None)

    # ---------- connection lifecycle ----------

    async def connect(self, name: str) -> bool:
        handle = self._servers.get(name)
        if handle is None:
            raise KeyError(f"unknown server '{name}'")
        if not handle.config.enabled:
            logger.info("server '%s' disabled, skipping connect", name)
            return False

        # Only hold lock to check/set state, not during I/O
        async with self._lock:
            if handle.connected:
                return True
            handle.connected = True # Mark as "in progress"

        try:
            if handle.config.transport == TransportType.STDIO:
                params = StdioServerParameters(
                    command=handle.config.command,
                    args=handle.config.args,
                    env=handle.config.env or None,
                )
                read, write = await handle.stack.enter_async_context(
                    stdio_client(params)
                )
            elif handle.config.transport == TransportType.SSE:
                headers = {}
                if handle.config.auth_token:
                    headers["Authorization"] = f"Bearer {handle.config.auth_token}"
                read, write = await handle.stack.enter_async_context(
                    sse_client(handle.config.url, headers=headers)
                )
            else:
                raise MCPConnectionError(f"unsupported transport for '{name}'")

            session = await handle.stack.enter_async_context(
                ClientSession(read, write)
            )
            await asyncio.wait_for(
                session.initialize(), timeout=handle.config.timeout_s
            )
            handle.session = session
            logger.info("connected to MCP server '%s'", name)

        except Exception as e:
            logger.warning("failed to connect to '%s': %s", name, e)
            await handle.stack.aclose()
            async with self._lock:
                handle.connected = False
                handle.session = None
            return False

        await self._discover_tools(handle)
        return True

    async def connect_all(self) -> dict[str, bool]:
        tasks = {name: self.connect(name) for name in self._servers.keys()}
        results = await asyncio.gather(*tasks.values())
        return dict(zip(tasks.keys(), results))

    async def disconnect(self, name: str) -> None:
        handle = self._servers.get(name)
        if handle is None or not handle.connected:
            return
        try:
            await handle.stack.aclose()
        except Exception as e:
            logger.warning("error during disconnect of '%s': %s", name, e)
        finally:
            handle.connected = False
            handle.session = None
            handle.tools.clear()

    async def disconnect_all(self) -> None:
        for name in list(self._servers.keys()):
            await self.disconnect(name)

    # ---------- discovery ----------

    async def _discover_tools(self, handle: _ServerHandle) -> None:
        if not handle.session:
            return
        try:
            result = await handle.session.list_tools()
            handle.tools = {
                t.name: ToolSpec(
                    server_name=handle.config.name,
                    name=t.name,
                    description=t.description or "",
                    input_schema=t.inputSchema or {},
                )
                for t in result.tools
            }
            logger.info(
                "discovered %d tools on '%s'", len(handle.tools), handle.config.name
            )
        except Exception as e:
            logger.warning("tool discovery failed for '%s': %s", handle.config.name, e)
            handle.tools = {}

    async def refresh_tools(self, name: Optional[str] = None) -> None:
        targets = [self._servers[name]] if name else list(self._servers.values())
        for handle in targets:
            if handle.connected:
                await self._discover_tools(handle)

    def list_tools(self) -> list[ToolSpec]:
        """All tools across all connected servers, qualified_name unique."""
        out = []
        for handle in self._servers.values():
            out.extend(handle.tools.values())
        return out

    def get_server_status(self) -> dict[str, bool]:
        return {name: h.connected for name, h in self._servers.items()}

    # ---------- tool invocation ----------

    async def call_tool(
        self, qualified_name: str, arguments: dict[str, Any]
    ) -> Any:
        """
        qualified_name is 'server:tool' (see ToolSpec.qualified_name).
        Raises MCPToolCallError on failure — caller decides fallback behavior.
        """
        if ":" not in qualified_name:
            raise MCPToolCallError(qualified_name, "expected 'server:tool' format")
        server_name, tool_name = qualified_name.split(":", 1)

        handle = self._servers.get(server_name)
        if handle is None or not handle.connected or not handle.session:
            raise MCPToolCallError(qualified_name, f"server '{server_name}' not connected")
        if tool_name not in handle.tools:
            raise MCPToolCallError(qualified_name, "tool not found in discovered set")

        try:
            result = await asyncio.wait_for(
                handle.session.call_tool(tool_name, arguments),
                timeout=handle.config.timeout_s,
            )
            if getattr(result, "isError", False):
                raise MCPToolCallError(qualified_name, str(result.content))
            return result.content
        except asyncio.TimeoutError:
            raise MCPToolCallError(qualified_name, "timed out")
        except MCPToolCallError:
            raise
        except Exception as e:
            raise MCPToolCallError(qualified_name, str(e))
