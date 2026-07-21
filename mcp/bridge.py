"""
nova/mcp/bridge.py
════════════════════
Bridges NOVA's synchronous tool dispatch (_execute_tool_sync, called from a
ThreadPoolExecutor by the Gemini Live event loop in live_extra.py) to the
async MCPClientManager.

Why a *second* event loop instead of reusing NOVALive's loop:
- NOVALive's asyncio loop is torn down and recreated across live/offline
  mode switches (see main()'s asyncio.run(nova.run()) called twice).
  MCP server connections should survive that switch — nobody wants to
  reconnect to every MCP server just because the internet blipped.
- Isolation also means an MCP server hang can't stall Gemini Live's
  receive loop; call_mcp_tool_sync() has its own timeout and always
  returns a string, mirroring ToolRegistry.execute()'s "never crashes"
  contract in tools/__init__.py.

Lifecycle:
    start_mcp_bridge(configs)   # call once during main(), before asyncio.run(nova.run())
    ... nova runs, tools get called via call_mcp_tool_sync() ...
    shutdown_mcp_bridge()       # call on process exit
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Optional

from .client_manager import MCPClientManager
from .models import ServerConfig

log = logging.getLogger("nova.mcp.bridge")

# name Gemini sees -> real "server:tool" qualified name.
# Gemini function names must be [a-zA-Z0-9_-]{1,64}, so "server:tool" (colon)
# is not valid — we expose "mcp__<server>__<tool>" instead.
_GEMINI_NAME_PREFIX = "mcp__"


class MCPBridge:
    def __init__(self):
        self._manager: Optional[MCPClientManager] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._name_map: dict[str, str] = {}  # gemini_name -> qualified_name

    # ---------- thread/loop management ----------

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def start(self, configs: list[ServerConfig], connect_timeout_s: float = 20.0) -> dict[str, bool]:
        """Blocking call — starts the MCP loop thread and connects all servers.
        Safe to call from main()'s synchronous startup path."""
        if self._thread is not None:
            log.warning("MCP bridge already started")
            return {}

        self._manager = MCPClientManager(configs)
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="MCPLoop")
        self._thread.start()
        self._ready.wait(timeout=5.0)

        fut = asyncio.run_coroutine_threadsafe(self._manager.connect_all(), self._loop)
        try:
            results = fut.result(timeout=connect_timeout_s)
        except Exception as e:
            log.error("MCP connect_all() timed out or failed: %s", e)
            results = {}

        self._rebuild_name_map()
        connected = [n for n, ok in results.items() if ok]
        failed = [n for n, ok in results.items() if not ok]
        if connected:
            log.info("MCP servers connected: %s", ", ".join(connected))
        if failed:
            log.warning("MCP servers failed to connect: %s", ", ".join(failed))
        return results

    def shutdown(self) -> None:
        if self._loop is None or self._manager is None:
            return
        fut = asyncio.run_coroutine_threadsafe(self._manager.disconnect_all(), self._loop)
        try:
            fut.result(timeout=10.0)
        except Exception as e:
            log.warning("MCP disconnect_all() error during shutdown: %s", e)
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5.0)
        self._thread = None
        self._loop = None

    # ---------- name mapping ----------

    def _rebuild_name_map(self) -> None:
        self._name_map.clear()
        if not self._manager:
            return
        for spec in self._manager.list_tools():
            gemini_name = f"{_GEMINI_NAME_PREFIX}{spec.server_name}__{spec.name}"
            self._name_map[gemini_name] = spec.qualified_name

    def is_mcp_tool(self, gemini_name: str) -> bool:
        return gemini_name in self._name_map

    # ---------- declarations (for TOOL_DECLARATIONS list in nova.py) ----------

    def gemini_declarations(self) -> list[dict[str, Any]]:
        """Gemini function_declarations format — same shape nova.py's
        TOOL_DECLARATIONS list already uses (name/description/parameters)."""
        if not self._manager:
            return []
        out = []
        for spec in self._manager.list_tools():
            gemini_name = f"{_GEMINI_NAME_PREFIX}{spec.server_name}__{spec.name}"
            out.append({
                "name": gemini_name,
                "description": f"[MCP:{spec.server_name}] {spec.description}"[:1000],
                "parameters": spec.input_schema or {"type": "object", "properties": {}},
            })
        return out

    # ---------- sync tool call (called from ThreadPoolExecutor worker) ----------

    def call_tool_sync(self, gemini_name: str, args: dict, timeout_s: float = 20.0) -> str:
        """Never raises — mirrors ToolRegistry.execute()'s error-as-string contract."""
        if self._loop is None or self._manager is None:
            return f"MCP tool '{gemini_name}' unavailable: bridge not started."
        qualified = self._name_map.get(gemini_name)
        if not qualified:
            return f"MCP tool '{gemini_name}' not found in current tool set."
        fut = asyncio.run_coroutine_threadsafe(
            self._manager.call_tool(qualified, args), self._loop
        )
        try:
            result = fut.result(timeout=timeout_s)
            return _stringify(result)
        except asyncio.TimeoutError:
            return f"MCP tool '{qualified}' timed out after {timeout_s}s."
        except Exception as e:
            log.error("MCP tool '%s' failed: %s", qualified, e)
            return f"MCP tool '{qualified}' error: {e}"

    def status(self) -> dict[str, bool]:
        return self._manager.get_server_status() if self._manager else {}


def _stringify(mcp_content: Any) -> str:
    """MCP call_tool results are a list of content blocks (text/image/etc).
    Collapse to a string for NOVA's tool contract, which is str-in/str-out."""
    if isinstance(mcp_content, str):
        return mcp_content
    if isinstance(mcp_content, list):
        parts = []
        for block in mcp_content:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(text)
            else:
                parts.append(str(block))
        return "\n".join(parts) if parts else "(empty result)"
    return str(mcp_content)


# ---------- module-level singleton, mirrors nova_state.py's pattern ----------
# nova.py should store this on nova_state._mcp_bridge rather than importing
# a global here directly — keeps nova_state.py as the single shared-state owner.

def new_bridge() -> MCPBridge:
    return MCPBridge()