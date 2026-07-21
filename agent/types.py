"""Shared types for the agent harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

TurnMessage = dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class GenerateResult:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def has_tools(self) -> bool:
        return bool(self.tool_calls)
