"""
NOVA Orchestrator — plan-to-graph execution with trust, confirmation and audit.

    Goal → Plan (graph) → per-step [Trust → Confirm → Capability → Verify]
                  → structured result + audit trail

This is additive to the existing runtime: the agent executor and the voice
loop can route through here without the live path changing.
"""
from orchestrator.audit_logger import AuditLogger
from orchestrator.orchestrator import (
    NOVAOrchestrator,
    OrchestrationResult,
    StepOutcome,
    StepRecord,
)
from orchestrator.plan import GoalPlan, GraphStep
from orchestrator.planner_adapter import (
    heuristic_plan,
    plan_to_graph,
    llm_capable_plan,
)
from orchestrator.shadow import (
    ShadowComparison,
    ShadowMonitor,
    default_shadow,
    install_shadow,
)

__all__ = [
    "AuditLogger",
    "NOVAOrchestrator",
    "OrchestrationResult",
    "StepOutcome",
    "StepRecord",
    "GoalPlan",
    "GraphStep",
    "heuristic_plan",
    "plan_to_graph",
    "llm_capable_plan",
    "ShadowMonitor",
    "ShadowComparison",
    "install_shadow",
    "default_shadow",
]