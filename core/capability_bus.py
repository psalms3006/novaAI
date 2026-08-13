"""
Capability bus — thin abstraction over NOVA's existing tool registry/dispatch.

This does not replace `_execute_tool_sync`; it adds:
- normalized capability metadata
- lookup by step description/agent
- risk/confirmation hints for the safety gate
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

log = logging.getLogger("nova.capabilities")

_CAPABILITIES = [
    {
        "name": "web_search",
        "description": "search the web for information",
        "agent": "research",
        "risk_level": "low",
        "requires_confirmation": False,
        "reversible": False,
        "verification_hint": "returned sources/links or summary",
    },
    {
        "name": "open_app",
        "description": "open a desktop application",
        "agent": "browser",
        "risk_level": "medium",
        "requires_confirmation": True,
        "reversible": False,
        "verification_hint": "app launched or error returned",
    },
    {
        "name": "file_processor",
        "description": "read, summarize, or extract text from files",
        "agent": "code",
        "risk_level": "low",
        "requires_confirmation": False,
        "reversible": False,
        "verification_hint": "file content snippet or summary returned",
    },
    {
        "name": "self_editor",
        "description": "edit project files safely with backup",
        "agent": "code",
        "risk_level": "medium",
        "requires_confirmation": True,
        "reversible": True,
        "verification_hint": "patch applied or diff shown",
    },
    {
        "name": "computer_settings",
        "description": "read or change system settings",
        "agent": "browser",
        "risk_level": "high",
        "requires_confirmation": True,
        "reversible": True,
        "verification_hint": "setting changed or current value reported",
    },
    {
        "name": "browser_control",
        "description": "control browser navigation and interaction",
        "agent": "browser",
        "risk_level": "medium",
        "requires_confirmation": False,
        "reversible": False,
        "verification_hint": "page snapshot or action confirmation",
    },
    {
        "name": "computer_control",
        "description": "direct mouse/keyboard control",
        "agent": "browser",
        "risk_level": "high",
        "requires_confirmation": True,
        "reversible": False,
        "verification_hint": "screenshot before/after or action confirmation",
    },
    {
        "name": "vision",
        "description": "capture or analyze screen/camera input",
        "agent": "vision",
        "risk_level": "low",
        "requires_confirmation": False,
        "reversible": False,
        "verification_hint": "image analysis result or saved path",
    },
    {
        "name": "planner",
        "description": "create or inspect task plans/reminders",
        "agent": "orchestrator",
        "risk_level": "low",
        "requires_confirmation": False,
        "reversible": True,
        "verification_hint": "planner task list updated",
    },
    {
        "name": "remember_fact",
        "description": "store a memory fact for later retrieval",
        "agent": "orchestrator",
        "risk_level": "low",
        "requires_confirmation": False,
        "reversible": True,
        "verification_hint": "fact count increased or confirmation returned",
    },
]


@dataclass
class Capability:
    name: str
    description: str
    agent: str
    risk_level: str
    requires_confirmation: bool
    reversible: bool
    verification_hint: str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Capability":
        return cls(**data)


class CapabilityBus:
    """Lookup layer between goal steps and NOVA tool dispatch."""

    def __init__(self, capabilities: Optional[list] = None) -> None:
        self._by_name: Dict[str, Capability] = {}
        self._by_agent: Dict[str, list] = {}
        for raw in capabilities or _CAPABILITIES:
            cap = Capability.from_dict(raw)
            self._by_name[cap.name] = cap
            self._by_agent.setdefault(cap.agent, []).append(cap)

    def get(self, name: str) -> Optional[Capability]:
        return self._by_name.get(name)

    def find_by_agent(self, agent: str) -> list:
        return list(self._by_agent.get(agent, []))

    def find_for_step(self, description: str, agent: Optional[str] = None) -> Optional[Capability]:
        text = description.lower()
        candidates = []
        for cap in self._by_name.values():
            if cap.name in text or any(kw in text for kw in cap.description.split()):
                candidates.append(cap)
        if agent:
            candidates = [c for c in candidates if c.agent == agent] + [c for c in candidates if c.agent != agent]
        return candidates[0] if candidates else None

    def all_tool_names(self) -> list:
        return list(self._by_name.keys())
