"""
NOVA Tool Registry — Safety-categorized tools with sentinel execution pattern.

Inspired by VYREN's tools/registry.py. Every tool has a safety_level:
  - "safe" tools execute immediately
  - "consequential" tools return _REQUESTED sentinels for post-confirmation execution

The registry never crashes — all errors are returned as text strings for the model.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("nova.tools")


# Sentinel values returned by consequential tools
SENTINEL_CONFIRM = "_REQUESTED"
CONFIRMATION_PATTERNS = {
    "shutdown": "SHUTDOWN_REQUESTED",
    "delete": "DELETE_REQUESTED",
    "restart": "RESTART_REQUESTED",
    "edit": "EDIT_REQUESTED",
    "write": "WRITE_REQUESTED",
    "patch": "PATCH_REQUESTED",
    "self_editor": "SELF_EDIT_REQUESTED",
}


@dataclass
class ToolDef:
    """Definition of a callable tool."""
    name: str
    description: str
    parameters: dict  # JSON Schema
    handler: Callable[[dict], str]
    safety_level: str = "safe"  # "safe" or "consequential"
    category: str = "general"  # For organization
    requires_online: bool = False
    requires_offline: bool = False


class ToolRegistry:
    """
    Central tool registry with safety-categorized execution.

    Usage:
        registry = ToolRegistry()
        registry.register(ToolDef(
            name="web_search",
            description="Search the web",
            parameters={...},
            handler=do_search,
            safety_level="safe",
            requires_online=True,
        ))
        result = registry.execute("web_search", {"query": "hello"})
    """

    def __init__(self) -> None:
        self._tools: Dict[str, ToolDef] = {}
        self._lock = threading.Lock()

    def register(self, tool: ToolDef) -> None:
        """Register a tool. Overwrites if name already exists."""
        with self._lock:
            self._tools[tool.name] = tool
            log.debug(f"Tool registered: {tool.name} (safety={tool.safety_level})")

    def unregister(self, name: str) -> None:
        with self._lock:
            self._tools.pop(name, None)

    def get(self, name: str) -> Optional[ToolDef]:
        with self._lock:
            return self._tools.get(name)

    def list_tools(self, safety_level: Optional[str] = None, category: Optional[str] = None) -> List[ToolDef]:
        """List tools, optionally filtered."""
        with self._lock:
            tools = list(self._tools.values())
        if safety_level:
            tools = [t for t in tools if t.safety_level == safety_level]
        if category:
            tools = [t for t in tools if t.category == category]
        return tools

    def get_gemini_declarations(self, exclude: Optional[List[str]] = None) -> List[dict]:
        """Get tool declarations in Gemini Live format."""
        exclude = exclude or []
        with self._lock:
            return [
                {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                }
                for t in self._tools.values()
                if t.name not in exclude
            ]

    def get_openai_definitions(self, exclude: Optional[List[str]] = None) -> List[dict]:
        """Get tool declarations in OpenAI function-calling format."""
        exclude = exclude or []
        with self._lock:
            return [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": self._to_openai_schema(t.parameters),
                    }
                }
                for t in self._tools.values()
                if t.name not in exclude
            ]

    @staticmethod
    def _to_openai_schema(gemini_schema: dict) -> dict:
        """Convert Gemini JSON Schema to OpenAI format."""
        props = gemini_schema.get("properties", {})
        return {
            "type": "object",
            "properties": {
                k: {"type": v.get("type", "string").lower(), "description": v.get("description", "")}
                for k, v in props.items()
            },
            "required": gemini_schema.get("required", []),
            "additionalProperties": False,
        }

    def execute(self, name: str, args: dict) -> str:
        """
        Execute a tool. Never crashes — errors returned as text.

        For consequential tools, returns sentinel values like
        "DELETE_REQUESTED: <description>" for the execution layer to handle.
        """
        tool = self.get(name)
        if tool is None:
            return f"Unknown tool: {name}"

        try:
            result = tool.handler(args)
            # Check if consequential tool returned a sentinel
            if isinstance(result, str) and result.endswith(SENTINEL_CONFIRM):
                log.info(f"Consequential tool '{name}' returned confirmation sentinel.")
            return result
        except Exception as e:
            error_msg = f"Tool '{name}' error: {e}"
            log.error(error_msg)
            return error_msg

    def needs_confirmation(self, result: str) -> bool:
        """Check if a tool result requires user confirmation."""
        return isinstance(result, str) and SENTINEL_CONFIRM in result

    def execute_post_confirmation(self, name: str, args: dict) -> str:
        """Execute a consequential tool after user confirmed."""
        return self.execute(name, args)

    def check_availability(self, is_online: bool) -> Dict[str, bool]:
        """Check which tools are available given current connectivity."""
        availability = {}
        with self._lock:
            for name, tool in self._tools.items():
                if tool.requires_online and not is_online:
                    availability[name] = False
                elif tool.requires_offline and is_online:
                    availability[name] = False
                else:
                    availability[name] = True
        return availability

    @property
    def tool_names(self) -> List[str]:
        with self._lock:
            return list(self._tools.keys())

    @property
    def count(self) -> int:
        return len(self._tools)