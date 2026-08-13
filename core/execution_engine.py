"""
Execution Engine — runs GoalSteps through NOVA's existing tool/agent dispatch.

Additive. Wraps existing tool/agent functions so goals can drive execution.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from core.goal_engine import GoalEngine, GoalStep
from core.capability_bus import CapabilityBus, Capability

log = logging.getLogger("nova.execution")


class StepOutcome:
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    BLOCKED = "BLOCKED"
    NEEDS_INPUT = "NEEDS_INPUT"


@dataclass
class StepResult:
    step_id: str
    status: str
    output: Optional[str]
    verification: Optional[str]
    outcome: str
    retry_count: int = 0
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "status": self.status,
            "output": self.output,
            "verification": self.verification,
            "outcome": self.outcome,
            "retry_count": self.retry_count,
            "error": self.error,
            "metadata": self.metadata,
        }


class ExecutionEngine:
    """
    Execute one goal step using NOVA's existing runtime.

    Parameters
    ----------
    goal_engine: GoalEngine
    capability_bus: CapabilityBus
    tool_fn: callable(tool_name, args, meta) -> str
    agent_fn: optional callable(agent, message, meta) -> str
    """

    def __init__(
        self,
        goal_engine: GoalEngine,
        capability_bus: Optional[CapabilityBus] = None,
        tool_fn: Optional[Callable[[str, Dict[str, Any], Dict[str, Any]], str]] = None,
        agent_fn: Optional[Callable[[str, str, Dict[str, Any]], str]] = None,
    ) -> None:
        self.goals = goal_engine
        self.bus = capability_bus or CapabilityBus()
        self.tool_fn = tool_fn
        self.agent_fn = agent_fn

    def execute_next_step(self, goal_id: str, meta: Optional[Dict[str, Any]] = None) -> Optional[StepResult]:
        goal = self.goals.store.get_goal(goal_id)
        if not goal:
            return None

        step = self.goals.get_next_step(goal_id)
        if not step:
            return None

        meta = meta or {}
        capability = self.bus.get(step.tool or "") if step.tool else None
        if capability is None:
            capability = self.bus.find_for_step(step.description, step.agent)

        try:
            if step.agent and self.agent_fn:
                raw_output = self.agent_fn(step.agent, step.description, meta)
            elif step.tool and self.tool_fn:
                raw_output = self.tool_fn(step.tool, step.input or {}, meta)
            else:
                raw_output = f"[no-op] {step.description}"
        except Exception as e:
            log.error("Step execution failed: %s", e)
            self.goals.fail_step(goal_id, step.step_id, str(e), retry=True)
            return StepResult(
                step_id=step.step_id,
                status="FAILED",
                output=None,
                verification=None,
                outcome=StepOutcome.FAILURE,
                error=str(e),
            )

        verification = self._verify(step, raw_output, capability)
        outcome = StepOutcome.SUCCESS if getattr(verification, "status", "") == "CONFIRMED_SUCCESS" else StepOutcome.BLOCKED

        step_status = step.status
        if outcome == StepOutcome.SUCCESS:
            updated = self.goals.complete_step(
                goal_id,
                step.step_id,
                output=raw_output,
                verification=getattr(verification, "reason", None),
                outcome=outcome,
            )
            if updated:
                plan_map = {s.step_id: s for s in updated.plan}
                step_status = plan_map.get(step.step_id, step).status
        else:
            self.goals.fail_step(goal_id, step.step_id, "verification did not confirm success", retry=True)
            step_status = "FAILED"

        self._store_outcome_memory(goal, step, outcome, raw_output)

        return StepResult(
            step_id=step.step_id,
            status=step_status,
            output=raw_output,
            verification=getattr(verification, "reason", None),
            outcome=outcome,
            metadata={"capability": capability.name if capability else None},
        )

    def _verify(self, step: GoalStep, raw_output: str, capability: Optional[Capability]) -> Any:
        from core.verification_engine import VerificationEngine
        verifier = VerificationEngine()
        return verifier.verify(step, raw_output, capability)

    def _store_outcome_memory(self, goal: Goal, step: GoalStep, outcome: str, raw_output: Optional[str]) -> None:
        try:
            add_memory_fact_fn = None
            try:
                from memory_extra import add_memory_fact as _add
                add_memory_fact_fn = _add
            except Exception:
                try:
                    import nova as _nova
                    add_memory_fact_fn = getattr(_nova, 'add_memory_fact', None)
                except Exception:
                    pass
            if add_memory_fact_fn is None:
                return
            meta = {
                user_name: goal.metadata.get(user_name, ),
                goal_id: goal.goal_id,
                step_id: step.step_id,
                agent: step.agent,
                tool: step.tool,
            }
            text = f"Outcome: {goal.mission} | step {step.step_id}: {outcome}"
            if raw_output:
                text += f" | {raw_output[:140]}"
            add_memory_fact_fn(text, meta)
        except Exception as memory_err:
            log.warning("Outcome memory store failed: %s", memory_err)


@dataclass
class GoalProgress:
    goal_id: str
    mission: str
    status: str
    completed_steps: int
    total_steps: int
    next_step: Optional[str]
    last_outcome: Optional[str]
    last_output: Optional[str]

    @classmethod
    def from_goal(cls, goal: Any) -> "GoalProgress":
        plan = getattr(goal, "plan", [])
        completed = sum(1 for s in plan if getattr(s, "status", "") == "COMPLETED")
        next_step = None
        for s in plan:
            if getattr(s, "status", "") == "PENDING":
                next_step = getattr(s, "step_id", None)
                break
        return cls(
            goal_id=getattr(goal, "goal_id", ""),
            mission=getattr(goal, "mission", ""),
            status=getattr(goal, "status", ""),
            completed_steps=completed,
            total_steps=len(plan),
            next_step=next_step,
            last_outcome=plan[-1].outcome if plan else None,
            last_output=plan[-1].output if plan else None,
        )
