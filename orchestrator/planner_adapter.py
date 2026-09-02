"""
Planner adapters — convert planner output into validated GoalPlans.

- ``plan_to_graph``        : NOVA planner JSON  -> GoalPlan (validated).
- ``heuristic_plan``       : offline keyword matching for fallback / tests.
- ``llm_capable_plan``     : optional Gemini plan with heuristic fallback.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

from capabilities.registry import CapabilityRegistry
from orchestrator.plan import GoalPlan, GraphStep

log = logging.getLogger("nova.orchestrator.planner_adapter")


def plan_to_graph(planner_json: Dict[str, Any], registry: Optional[CapabilityRegistry] = None) -> GoalPlan:
    """Convert a NOVA planner JSON dict into a validated GoalPlan.

    Steps referencing unknown capabilities are preserved but marked — the
    orchestrator resolves availability at execution time. The step count is
    capped to avoid runaway multi-step plans.
    """
    goal = str(planner_json.get("goal", "") or "")
    raw_steps = planner_json.get("steps", []) or []
    steps: list[GraphStep] = []

    for idx, raw in enumerate(raw_steps, start=1):
        if not isinstance(raw, dict):
            continue
        tool = str(raw.get("tool", "")).strip()
        if not tool:
            continue
        steps.append(
            GraphStep(
                step_id=str(raw.get("step", f"s{idx}")),
                tool=tool,
                description=str(raw.get("description", "") or tool),
                parameters=dict(raw.get("parameters", {}) or {}),
                critical=bool(raw.get("critical", False)),
            )
        )
        if len(steps) >= 20:  # safety cap
            break

    return GoalPlan(goal=goal, steps=steps)


def heuristic_plan(goal: str, registry: CapabilityRegistry) -> GoalPlan:
    """Offline keyword-based planning (fallback and test path)."""
    spec = registry.find_for_intent(goal)
    tool = spec.name if spec is not None else "web_search"
    params: Dict[str, Any] = {}
    if tool == "web_search":
        params["query"] = goal

    return GoalPlan(
        goal=goal,
        steps=[GraphStep(step_id="s1", tool=tool, description=f"Handle: {goal}", parameters=params)],
    )


def llm_capable_plan(
    goal: str,
    registry: CapabilityRegistry,
    api_key_path: Optional[str] = None,
    model: str = "gemini-2.5-flash-lite",
) -> GoalPlan:
    """LLM planning with automatic fallback to the heuristic plan.

    Uses NOVA's existing Gemini REST planning path (``agent.planner``). If
    that path is unavailable or malformed, falls back to ``heuristic_plan``
    so planning never crashes the orchestrator.
    """
    try:
        from agent.planner import create_plan

        planner_json = create_plan(goal)
        plan = plan_to_graph(planner_json, registry)
        if not plan.is_empty:
            return plan
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM planning failed (%s) — falling back to heuristic plan.", exc)

    return heuristic_plan(goal, registry)


def plan_from_text(text: str, registry: CapabilityRegistry) -> GoalPlan:
    """Best-effort: try to parse a raw JSON plan blob, else heuristic."""
    cleaned = re.sub(r"```(?:json)?", "", text).strip().rstrip("`").strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return heuristic_plan(text, registry)
    return plan_to_graph(data, registry)