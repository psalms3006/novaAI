"""
NOVA Orchestrator — runs a GoalPlan through trust, confirmation, capability
execution and verification, producing a structured result + audit trail.

The orchestrator never executes a capability without first resolving its
metadata (risk/policy) through the TrustEngine. A ``ConfirmationGate`` (which
# MAY be fully headless) is the only way a human enters the loop.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from time import perf_counter
from typing import Any, Callable, Dict, List, Optional

from capabilities.contracts import CapabilityContext, CapabilitySpec
from capabilities.registry import (
    CapabilityNotFoundError,
    CapabilityRegistry,
    CapabilityUnavailableError,
    build_core_registry,
)
from core.verification_engine import Verification, VerificationEngine
from orchestrator.audit_logger import AuditLogger
from orchestrator.plan import GoalPlan, GraphStep
from orchestrator.planner_adapter import heuristic_plan
from trust.confirmation import ConfirmationGate, ConfirmationRequester
from trust.trust_engine import TrustDecision, TrustDecisionKind, TrustEngine

log = logging.getLogger("nova.orchestrator")

PlannerFn = Callable[[str], GoalPlan]
ResponderFn = Callable[[str, List["StepRecord"]], str]


class StepOutcome(Enum):
    PENDING = "pending"
    EXECUTING = "executing"
    CONFIRMING = "confirming"
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class StepRecord:
    step_id: str
    tool: str
    description: str
    decision: Optional[str] = None
    outcome: StepOutcome = StepOutcome.PENDING
    output: Optional[str] = None
    error: Optional[str] = None
    latency_ms: float = 0.0
    verification: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "tool": self.tool,
            "description": self.description,
            "decision": self.decision,
            "outcome": self.outcome.value,
            "output": self.output,
            "error": self.error,
            "latency_ms": round(self.latency_ms, 1),
            "verification": self.verification,
        }


@dataclass
class OrchestrationResult:
    goal: str
    status: str  # SUCCEEDED | PARTIAL | BLOCKED | FAILED
    steps: List[StepRecord] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal": self.goal,
            "status": self.status,
            "summary": self.summary,
            "steps": [s.to_dict() for s in self.steps],
        }


class NOVAOrchestrator:
    """Drives a goal through validated capability execution."""

    def __init__(
        self,
        registry: Optional[CapabilityRegistry] = None,
        trust: Optional[TrustEngine] = None,
        gate: Optional[ConfirmationGate] = None,
        audit: Optional[AuditLogger] = None,
        event_bus: Optional[Any] = None,
        verifier: Optional[VerificationEngine] = None,
        planner: Optional[PlannerFn] = None,
        responder: Optional[ResponderFn] = None,
    ) -> None:
        self.registry = registry or build_core_registry()
        self.trust = trust or TrustEngine()
        self.gate = gate or ConfirmationGate()
        self.audit = audit or AuditLogger()
        self.bus = event_bus
        self.verifier = verifier
        self.custom_planner = planner
        self.responder = responder

    # ── public ────────────────────────────────────────────────────────

    def run(
        self,
        goal: str,
        *,
        platform: str = "windows",
        connectivity: Any = None,
        speak: Optional[Callable[[str], None]] = None,
        confidence: float = 0.7,
        ambiguous: bool = False,
        force_confirm: bool = False,
        session_facts: Optional[List[str]] = None,
        cancel: Optional[threading.Event] = None,
    ) -> OrchestrationResult:
        plan = self._plan(goal)
        if plan.is_empty:
            self.audit.record("goal.no_plan", goal=goal)
            return OrchestrationResult(goal=goal, status="FAILED", summary="I couldn't form a plan for that request.")

        self._publish("orchestrator.goal.start", {"goal": goal, "steps": len(plan.steps)})
        self.audit.record("goal.start", goal=goal, steps=len(plan.steps))

        records: List[StepRecord] = []
        aborted = False

        for step in plan.steps:
            if cancel is not None and cancel.is_set():
                records.append(StepRecord(step.step_id, step.tool, step.description, outcome=StepOutcome.CANCELLED, error="cancelled"))
                aborted = True
                break

            record = self._run_step(
                step,
                platform=platform,
                connectivity=connectivity,
                speak=speak,
                confidence=confidence,
                ambiguous=ambiguous,
                force_confirm=force_confirm,
                session_facts=session_facts,
            )
            records.append(record)
            self._publish("orchestrator.step", {"step_id": step.step_id, "tool": step.tool, "outcome": record.outcome.value})
            if record.outcome in (StepOutcome.BLOCKED, StepOutcome.FAILED) and step.critical:
                aborted = True
                break
            if record.outcome is StepOutcome.CANCELLED:
                aborted = True
                break

        status = self._status(records, aborted)
        summary = self._summarize(goal, records, status)

        self.audit.record("goal.finish", goal=goal, status=status, steps=len(records))
        self._publish("orchestrator.goal.finish", {"goal": goal, "status": status})
        return OrchestrationResult(goal=goal, status=status, steps=records, summary=summary)

    # ── planning ──────────────────────────────────────────────────────

    def _plan(self, goal: str) -> GoalPlan:
        if self.custom_planner is not None:
            return self.custom_planner(goal)
        return heuristic_plan(goal, self.registry)

    # ── step execution ────────────────────────────────────────────────

    def _run_step(
        self,
        step: GraphStep,
        *,
        platform: str,
        connectivity: Any,
        speak: Optional[Callable[[str], None]],
        confidence: float,
        ambiguous: bool,
        force_confirm: bool,
        session_facts: Optional[List[str]],
    ) -> StepRecord:
        start = perf_counter()
        record = StepRecord(step.step_id, step.tool, step.description)

        spec = self.registry.get(step.tool)
        action = step.parameters.get("action")

        if spec is None:
            # No metadata → no way to risk-assess → never fabricate a decision.
            record.decision = "unknown-capability"
            record.outcome = StepOutcome.BLOCKED
            record.error = f"No capability registered for '{step.tool}'."
            record.latency_ms = (perf_counter() - start) * 1000
            self._audit_step(record, step)
            return record

        decision = self.trust.decide(
            spec, action=action, confidence=confidence, ambiguous=ambiguous, forced=force_confirm
        )
        record.decision = decision.kind.value
        record.verification = decision.reason

        if decision.kind is TrustDecisionKind.BLOCK:
            record.outcome = StepOutcome.BLOCKED
            record.error = decision.reason
            self._finish(record, start)
            return record

        granted = False
        if decision.kind is TrustDecisionKind.EXECUTE:
            granted = True
        else:
            granted, needs_input = self.gate.allowed(spec, step.parameters, decision)
            if not granted:
                record.outcome = StepOutcome.BLOCKED
                record.error = (
                    "Confirmation required but no user interface attached - blocking."
                    if needs_input
                    else "User declined confirmation."
                )
                self.trust.learn_from_confirmation(spec, action, confirmed=False)
                self._finish(record, start)
                return record
            self.trust.learn_from_confirmation(spec, action, confirmed=True)

        # Connectivity gate for online-required capabilities.
        ctx = CapabilityContext(
            platform=platform,
            speak=speak,
            connectivity=connectivity,
            session_facts=list(session_facts or []),
        )
        if spec is not None and spec.requires_internet and not ctx.is_online:
            record.outcome = StepOutcome.BLOCKED
            record.error = f"'{step.tool}' needs internet and NOVA is offline."
            self._finish(record, start, ctx, step)
            return record

        try:
            output = self.registry.execute(step.tool, step.parameters, ctx)
        except CapabilityNotFoundError as exc:
            record.outcome = StepOutcome.BLOCKED
            record.error = str(exc)
            self._finish(record, start, ctx, step)
            return record
        except CapabilityUnavailableError as exc:
            record.outcome = StepOutcome.BLOCKED
            record.error = str(exc)
            self._finish(record, start, ctx, step)
            return record
        except Exception as exc:  # noqa: BLE001 — a tool failure must not crash the loop
            record.outcome = StepOutcome.FAILED
            record.error = str(exc)
            log.exception("Step %s failed", step.tool)
            self._finish(record, start, ctx, step)
            return record

        record.output = output

        if self.verifier is not None:
            verification = self.verifier.verify(step, output, spec)
            record.verification = f"{verification.status}: {verification.reason}"
            if verification.status == "FAILURE":
                record.outcome = StepOutcome.BLOCKED
                record.error = f"verification failed: {verification.reason}"
                self._finish(record, start, ctx, step)
                return record

        record.outcome = StepOutcome.SUCCEEDED
        self._finish(record, start, ctx, step)
        return record

    def _finish(
        self,
        record: StepRecord,
        start: float,
        ctx: Optional[CapabilityContext] = None,
        step: Optional[GraphStep] = None,
    ) -> None:
        record.latency_ms = (perf_counter() - start) * 1000
        if step is not None:
            spec = self.registry.get(step.tool)
            risk = spec.risk_for_action(step.parameters.get("action")).value if spec else None
            self._audit_step(record, step, risk=risk)
        if ctx is not None and record.outcome in (
            StepOutcome.SUCCEEDED,
            StepOutcome.BLOCKED,
            StepOutcome.FAILED,
        ):
            output = record.output or record.error or ""
            self._publish(
                "orchestrator.step.result",
                {"tool": record.tool, "outcome": record.outcome.value, "output": output[:200]},
            )

    # ── status + summary ──────────────────────────────────────────────

    def _status(self, records: List[StepRecord], aborted: bool) -> str:
        if not records:
            return "FAILED"
        if aborted and any(r.outcome is StepOutcome.SUCCEEDED for r in records):
            return "PARTIAL"
        if all(r.outcome is StepOutcome.SUCCEEDED for r in records):
            return "SUCCEEDED"
        if any(r.outcome is StepOutcome.CANCELLED for r in records):
            return "CANCELLED"
        if any(r.outcome is StepOutcome.BLOCKED for r in records):
            return "BLOCKED"
        return "FAILED"

    def _summarize(self, goal: str, records: List[StepRecord], status: str) -> str:
        if self.responder is not None:
            try:
                return self.responder(goal, records)
            except Exception as exc:  # noqa: BLE001
                log.warning("Responder failed (%s) — using default summary.", exc)

        if status in ("BLOCKED", "CANCELLED"):
            blocked = [r for r in records if r.outcome in (StepOutcome.BLOCKED, StepOutcome.CANCELLED)]
            if blocked:
                name = blocked[0].tool
                reason = blocked[0].error or ""
                if status == "CANCELLED":
                    return "Task cancelled."
                return f"{name} was blocked" + (f": {reason}" if reason else ".")
        if status == "FAILED":
            failed = [r for r in records if r.outcome is StepOutcome.FAILED]
            if failed:
                return f"That didn't work - {failed[0].tool} failed: {failed[0].error}"
            return "That didn't work."
        if not records:
            return f"Done with {goal}."

        outputs = [r.output for r in records if r.output]
        if len(records) == 1 and outputs:
            first = outputs[0]
            return first if len(first) <= 400 else first[:400] + "..."
        completed = sum(1 for r in records if r.outcome is StepOutcome.SUCCEEDED)
        return f"Completed {completed}/{len(records)} steps" + (f" for '{goal[:80]}'." if goal else ".")

    # ── observability ─────────────────────────────────────────────────

    def _audit_step(self, record: StepRecord, step: GraphStep, risk: Optional[str] = None) -> None:
        self.audit.record(
            "step.result",
            goal=step.description,
            step_id=record.step_id,
            tool=record.tool,
            risk=risk,
            decision=record.decision,
            outcome=record.outcome.value,
            latency_ms=round(record.latency_ms, 1),
            error=(record.error or "")[:120] if record.error else None,
        )

    def _publish(self, event_type: str, data: Dict[str, Any]) -> None:
        if self.bus is None:
            return
        try:
            from core.event_bus import Event

            self.bus.publish(Event(type=event_type, source="orchestrator", data=data))
        except Exception as exc:  # noqa: BLE001
            log.debug("event publish failed: %s", exc)