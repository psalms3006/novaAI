"""
Goal Engine — persistent, goal-driven execution tracking for NOVA.

Provides:
- Goal / GoalStep / GoalPlan models
- GoalStore: atomic JSON persistence for active goals
- GoalEngine: create, continue, complete, fail, block, unblock, list goals

This is additive and does not change existing production paths.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger("nova.goal")

_GOALS_PATH = "goals.json"


class GoalStatus:
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass
class GoalStep:
    step_id: str
    description: str
    status: str = "PENDING"
    tool: Optional[str] = None
    agent: Optional[str] = None
    input: Optional[Dict[str, Any]] = None
    output: Optional[str] = None
    error: Optional[str] = None
    verification: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 2
    outcome: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "description": self.description,
            "status": self.status,
            "tool": self.tool,
            "agent": self.agent,
            "input": self.input,
            "output": self.output,
            "error": self.error,
            "verification": self.verification,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "outcome": self.outcome,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GoalStep":
        return cls(**data)


@dataclass
class Goal:
    goal_id: str
    mission: str
    status: str = GoalStatus.ACTIVE
    plan: List[GoalStep] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "mission": self.mission,
            "status": self.status,
            "plan": [step.to_dict() for step in self.plan],
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Goal":
        data = dict(data)
        data["plan"] = [GoalStep.from_dict(s) for s in data.get("plan", [])]
        return cls(**data)


def _atomic_write(path: str, data: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


class GoalStore:
    """Persistent store for goals with atomic writes."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or _GOALS_PATH
        self._data: Dict[str, Any] = {"goals": {}}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except Exception as e:
            log.warning("GoalStore load failed: %s", e)
            self._data = {"goals": {}}

    def _save(self) -> None:
        _atomic_write(self.path, self._data)

    def get_goal(self, goal_id: str) -> Optional[Goal]:
        raw = self._data.get("goals", {}).get(goal_id)
        if not raw:
            return None
        return Goal.from_dict(raw)

    def list_goals(self, status: Optional[str] = None) -> List[Goal]:
        goals = [Goal.from_dict(g) for g in self._data.get("goals", {}).values()]
        if status:
            goals = [g for g in goals if g.status == status]
        return sorted(goals, key=lambda g: g.updated_at, reverse=True)

    def save_goal(self, goal: Goal) -> Goal:
        goals = self._data.setdefault("goals", {})
        goal.updated_at = time.time()
        goals[goal.goal_id] = goal.to_dict()
        self._save()
        return goal

    def update_step(self, goal_id: str, step_id: str, **fields: Any) -> Optional[Goal]:
        goal = self.get_goal(goal_id)
        if not goal:
            return None
        for step in goal.plan:
            if step.step_id == step_id:
                for k, v in fields.items():
                    if hasattr(step, k):
                        setattr(step, k, v)
                step.updated_at = time.time()
                break
        goal.updated_at = time.time()
        return self.save_goal(goal)


class GoalEngine:
    """Goal lifecycle manager."""

    def __init__(self, store: Optional[GoalStore] = None) -> None:
        self.store = store or GoalStore()

    def create_goal(self, mission: str, plan: Optional[List[Dict[str, Any]]] = None) -> Goal:
        goal_id = "goal_" + str(int(time.time() * 1000))
        goal = Goal(goal_id=goal_id, mission=mission)
        if plan:
            goal.plan = [GoalStep(**step) for step in plan]
        else:
            goal.plan = [GoalStep(step_id="s1", description=mission)]
        return self.store.save_goal(goal)

    def get_next_step(self, goal_id: str) -> Optional[GoalStep]:
        goal = self.store.get_goal(goal_id)
        if not goal:
            return None
        for step in goal.plan:
            if step.status == "PENDING":
                return step
        return None

    def complete_step(self, goal_id: str, step_id: str, output: Optional[str] = None,
                      verification: Optional[str] = None, outcome: Optional[str] = None) -> Optional[Goal]:
        updated = self.store.update_step(
            goal_id,
            step_id,
            status="COMPLETED",
            output=output,
            verification=verification,
            outcome=outcome,
            completed_at=time.time(),
        )
        if not updated:
            return None
        if all(s.status == "COMPLETED" for s in updated.plan):
            updated.status = GoalStatus.COMPLETED
            updated.completed_at = time.time()
            return self.store.save_goal(updated)
        return updated

    def fail_step(self, goal_id: str, step_id: str, error: str, retry: bool = True) -> Optional[Goal]:
        goal = self.store.get_goal(goal_id)
        if not goal:
            return None
        for step in goal.plan:
            if step.step_id == step_id:
                step.error = error
                step.retry_count += 1
                if retry and step.retry_count < step.max_retries:
                    step.status = "PENDING"
                else:
                    step.status = "FAILED"
                    goal.status = GoalStatus.BLOCKED
                break
        goal.updated_at = time.time()
        return self.store.save_goal(goal)

    def block_goal(self, goal_id: str, reason: str) -> Optional[Goal]:
        goal = self.store.get_goal(goal_id)
        if not goal:
            return None
        goal.status = GoalStatus.BLOCKED
        goal.metadata["blocker"] = reason
        goal.updated_at = time.time()
        return self.store.save_goal(goal)

    def unblock_goal(self, goal_id: str) -> Optional[Goal]:
        goal = self.store.get_goal(goal_id)
        if not goal:
            return None
        goal.status = GoalStatus.ACTIVE
        goal.metadata.pop("blocker", None)
        goal.updated_at = time.time()
        return self.store.save_goal(goal)

    def recover_unfinished(self) -> List[Goal]:
        return [g for g in self.store.list_goals() if g.status in {
            GoalStatus.ACTIVE, GoalStatus.PAUSED, GoalStatus.BLOCKED
        }]
