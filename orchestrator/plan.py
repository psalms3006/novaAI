"""
Plan data structures — a goal decomposed into an ordered graph of steps.

Steps are validated capabilities (never free-form LLM commands). Each step
resolves to a concrete capability name; the orchestrator resolves metadata,
applies trust/confirmation, executes and verifies.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class GraphStep:
    step_id: str
    tool: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    critical: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "tool": self.tool,
            "description": self.description,
            "parameters": self.parameters,
            "critical": self.critical,
        }


@dataclass
class GoalPlan:
    goal: str
    steps: List[GraphStep] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.steps

    def to_dict(self) -> Dict[str, Any]:
        return {"goal": self.goal, "steps": [s.to_dict() for s in self.steps]}