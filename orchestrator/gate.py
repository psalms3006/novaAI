"""
GatedToolRunner — enforce the orchestrator's trust/confirm/verify rules on a
single tool call, without a full plan.

This is the switchover mechanism: instead of the agent executor (or the live
dispatch) running every tool unconditionally, an opt-in gate makes each call
go through TrustEngine -> ConfirmationGate -> dispatch -> VerificationEngine
first. It is the same pipeline ``NOVAOrchestrator`` uses, applied to the
existing dispatch surface.

Safety rules (mirroring the orchestrator):
- No metadata -> blocked (never fabricate a decision).
- BLOCK decision -> blocked. CONFIRM without an approving requester -> blocked.
- The capability is dispatched at most once, and only after a green decision.
- Verification FAILURE -> blocked, even if dispatch "succeeded".
- Dispatch exceptions propagate unchanged (preserves legacy error recovery).
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional

from capabilities.contracts import CapabilitySpec
from capabilities.registry import CapabilityRegistry, build_core_registry
from core.verification_engine import VerificationEngine
from orchestrator.audit_logger import AuditLogger
from trust.confirmation import ConfirmationGate
from trust.trust_engine import TrustDecisionKind, TrustEngine

log = logging.getLogger("nova.orchestrator.gate")


class GatedToolRunner:
    def __init__(
        self,
        registry: Optional[CapabilityRegistry] = None,
        trust: Optional[TrustEngine] = None,
        gate: Optional[ConfirmationGate] = None,
        audit: Optional[AuditLogger] = None,
        verifier: Optional[VerificationEngine] = None,
    ) -> None:
        self.registry = registry or build_core_registry()
        self.trust = trust or TrustEngine()
        self.gate = gate or ConfirmationGate()
        self.audit = audit or AuditLogger()
        self.verifier = verifier or VerificationEngine()

    def run(
        self,
        tool: str,
        parameters: Dict[str, Any],
        dispatch: Callable[[], str],
        *,
        speak: Optional[Callable[[str], None]] = None,
        goal: str = "",
    ) -> str:
        """Run one tool call behind the gate. Returns the result or a block message."""
        spec = self.registry.get(tool)
        action = parameters.get("action")
        risk = spec.risk_for_action(action).value if spec else None

        if spec is None:
            msg = f"I won't run '{tool}' - it has no risk metadata."
            self.audit.record("gate.blocked", tool=tool, reason="no-metadata", goal=goal[:200])
            return msg

        decision = self.trust.decide(spec, action=action)
        if decision.kind is TrustDecisionKind.BLOCK:
            self.audit.record(
                "gate.blocked", tool=tool, action=action, risk=risk,
                decision=decision.kind.value, reason=decision.reason, goal=goal[:200],
            )
            return f"I won't run '{tool}': {decision.reason}"

        if decision.kind is TrustDecisionKind.EXECUTE:
            granted = True
        else:
            granted, needs_input = self.gate.allowed(spec, parameters, decision)
            if not granted:
                reason = (
                    "I need your confirmation and no confirmation channel is attached."
                    if needs_input else "You declined confirmation."
                )
                self.audit.record(
                    "gate.blocked", tool=tool, action=action, risk=risk,
                    decision=decision.kind.value, reason=reason, goal=goal[:200],
                )
                return f"I won't run '{tool}': {reason}"
            self.trust.learn_from_confirmation(spec, action, confirmed=True)

        outcome = dispatch()  # exceptions propagate for legacy error recovery

        verification = self.verifier.verify(SimpleNamespace(tool=tool, description=goal or tool), outcome, spec)
        status = verification.status
        ok = status != "FAILURE"

        self.audit.record(
            "gate.executed",
            tool=tool, action=action, risk=risk,
            decision=decision.kind.value, outcome="success" if ok else "blocked",
            verification=status, reason=verification.reason,
            goal=goal[:200],
        )
        if not ok:
            return f"'{tool}' ran but its result failed verification: {verification.reason}"
        return outcome