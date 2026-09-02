"""
Shadow Monitor — observe the legacy tool path without changing its behavior.

Purpose
-------
Before we switch the live loop over to ``NOVAOrchestrator``, we run every
legacy tool call through a *shadow* of the trust/verification rules:
metadata resolution → TrustEngine decision → VerificationEngine on the real
output. The shadow never executes the capability a second time and never asks
the user anything — it only records, for each call, what the orchestrator
``WOULD`` have decided, and a verdict comparing it to what the legacy path
actually did.

Verdicts
--------
- ``match``               : orchestrator would execute, and it ran and verified.
- ``match-but-failed``    : orchestrator would execute, tool itself failed.
- ``mismatch-unsafe``     : legacy ran a capability the orchestrator would
                            CONFIRM or BLOCK. Highest-priority signal.
- ``no-metadata``         : no CapabilitySpec exists for this tool — the
                            trust rules cannot assess it (a blind spot).

The aggregate ``report()`` drives the kill-switch decision: repeated
``mismatch-unsafe`` entries argue for gating those tools before switchover.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from time import perf_counter
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional, Protocol

from capabilities.registry import CapabilityRegistry, build_core_registry
from core.verification_engine import VerificationEngine
from orchestrator.audit_logger import AuditLogger
from trust.trust_engine import TrustDecisionKind, TrustEngine

log = logging.getLogger("nova.orchestrator.shadow")


class ToolCall(Protocol):
    """The legacy dispatch closure: run the real tool, return its text."""

    def __call__(self) -> str:
        ...


@dataclass
class ShadowComparison:
    tool: str
    shadow_decision: str
    legacy_outcome: str
    verification: str
    risk: Optional[str] = None
    verdict: str = ""
    latency_ms: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "shadow_decision": self.shadow_decision,
            "legacy_outcome": self.legacy_outcome,
            "verification": self.verification,
            "risk": self.risk,
            "verdict": self.verdict,
            "latency_ms": round(self.latency_ms, 1),
            "error": self.error,
        }


class ShadowMonitor:
    """Observes legacy tool calls and records orchestrator-shadow comparisons."""

    def __init__(
        self,
        registry: Optional[CapabilityRegistry] = None,
        trust: Optional[TrustEngine] = None,
        audit: Optional[AuditLogger] = None,
        verifier: Optional[VerificationEngine] = None,
        confidence: float = 0.7,
    ) -> None:
        self.registry = registry or build_core_registry()
        self.trust = trust or TrustEngine()
        self.audit = audit or AuditLogger()
        self.verifier = verifier or VerificationEngine()
        self.confidence = confidence
        self._counts: Dict[str, int] = {}

    # ── public ──────────────────────────────────────────────────────────

    def watch(
        self,
        tool: str,
        parameters: Dict[str, Any],
        call: ToolCall,
        *,
        speak: Optional[Callable[[str], None]] = None,
        connectivity: Any = None,
        goal: str = "",
    ) -> str:
        """Run the real legacy call, shadow-decide around it, audit, return.

        The legacy result is preserved exactly — on failure the original
        exception is re-raised after the comparison has been recorded.
        """
        start = perf_counter()
        spec = self.registry.get(tool)
        action = parameters.get("action")

        shadow_decision = "unknown"
        risk = None
        if spec is not None:
            decision = self.trust.decide(spec, action=action, confidence=self.confidence)
            shadow_decision = decision.kind.value
            risk = spec.risk_for_action(action).value

        error: Optional[str] = None
        output: Optional[str] = None
        legacy_outcome = "failure"
        try:
            output = str(call())
            legacy_outcome = "success"
        except Exception as exc:  # noqa: BLE001 — preserve legacy exception
            error = str(exc)
            raise
        finally:
            self._record(
                tool=tool,
                parameters=parameters,
                goal=goal,
                shadow_decision=shadow_decision,
                legacy_outcome=legacy_outcome,
                output=output,
                risk=risk,
                has_spec=spec is not None,
                error=error,
                latency=(perf_counter() - start) * 1000,
            )
        return output

    def report(self) -> Dict[str, Any]:
        """Aggregate verdict counts across every comparison recorded this run."""
        totals = {
            "match": 0,
            "match-but-failed": 0,
            "mismatch-unsafe": 0,
            "no-metadata": 0,
            **self._counts,
        }
        return dict(totals)

    # ── internals ───────────────────────────────────────────────────────

    def _record(
        self,
        *,
        tool: str,
        parameters: Dict[str, Any],
        goal: str,
        shadow_decision: str,
        legacy_outcome: str,
        output: Optional[str],
        risk: Optional[str],
        has_spec: bool,
        error: Optional[str],
        latency: float,
    ) -> None:
        verification = "disabled"
        if self.verifier is not None and output is not None:
            step = SimpleNamespace(tool=tool, description=(goal or tool))
            capability = self.registry.get(tool)  # has .verification_hint
            verification = f"{self.verifier.verify(step, output, capability).status}"

        verdict = self._verdict(has_spec, shadow_decision, legacy_outcome, verification)

        self._counts[verdict] = self._counts.get(verdict, 0) + 1

        warning = verdict == "mismatch-unsafe"
        self.audit.record(
            "shadow.compare",
            warning=warning,
            goal=goal[:200],
            tool=tool,
            action=parameters.get("action"),
            risk=risk,
            shadow_decision=shadow_decision,
            legacy_outcome=legacy_outcome,
            verification=verification,
            verdict=verdict,
            latency_ms=round(latency, 1),
            error=(error or "")[:140] if error else None,
        )

        level = log.warning if warning else log.debug
        level("shadow %s %s decision=%s legacy=%s verdict=%s", tool, risk or "n/a", shadow_decision, legacy_outcome, verdict)

    @staticmethod
    def _verdict(
        has_spec: bool,
        shadow_decision: str,
        legacy_outcome: str,
        verification: str,
    ) -> str:
        if not has_spec:
            return "no-metadata"
        if shadow_decision != TrustDecisionKind.EXECUTE.value:
            return "mismatch-unsafe"
        if legacy_outcome != "success":
            return "match-but-failed"
        if verification == "FAILURE":
            return "match-but-failed"
        return "match"


def install_shadow(monitor: Optional[ShadowMonitor]) -> None:
    """Set the module-level shadow used by the agent executor (None = off)."""
    from agent import executor as _executor

    _executor._set_shadow(monitor)
    if monitor is None:
        log.info("Shadow monitoring disabled.")
    else:
        log.info("Shadow monitoring enabled on the live tool path.")


def default_shadow() -> ShadowMonitor:
    """A pre-wired monitor for boot-time installation."""
    return ShadowMonitor()