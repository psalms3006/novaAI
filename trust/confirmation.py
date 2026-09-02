"""
Confirmation gate — bridges trust decisions to voice/UI confirmation.

A ``ConfirmationRequester`` is the only thing that can attach a real human
to the loop (stdin, voice, tray, mobile push). When no requester is attached
and a trust decision needs confirmation, the gate blocks (returns False /
needs_input=True) rather than silently approving.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Protocol

from capabilities.contracts import CapabilitySpec
from trust.trust_engine import TrustDecision, TrustDecisionKind

log = logging.getLogger("nova.trust.confirmation")

_AFFIRMATIVE = {"yes", "yeah", "yep", "sure", "ok", "okay", "go", "proceed", "do it", "confirm"}


class ConfirmationRequester(Protocol):
    """Ask the user a yes/no question; returns their raw text reply."""

    def ask(self, question: str) -> str:
        ...


class TextConfirmationRequester:
    """Blocking stdin confirmation (text mode)."""

    def __init__(self, read_line: Optional[Callable[[], str]] = None) -> None:
        self._read = read_line or input  # type: ignore[assignment]

    def ask(self, question: str) -> str:
        try:
            return str(self._read(f"{question} (yes/no) ")).strip()
        except (EOFError, KeyboardInterrupt):
            return ""


class MockConfirmationRequester:
    """Deterministic requester for tests / headless automation."""

    def __init__(self, reply: str = "yes") -> None:
        self.reply = reply
        self.asked: list[str] = []

    def ask(self, question: str) -> str:
        self.asked.append(question)
        return self.reply


class NoOpConfirmationRequester:
    """Meaningful default when no user is attached — always declines."""

    def ask(self, question: str) -> str:
        return ""


def parse_confirmation(reply: str) -> bool:
    return any(w in reply.lower() for w in _AFFIRMATIVE)


@dataclass
class ConfirmationGate:
    """Decides whether a trust decision may proceed to execution."""

    requester: Optional[ConfirmationRequester] = None
    speak: Optional[Callable[[str], None]] = None

    def allowed(
        self,
        spec: CapabilitySpec,
        args: Dict[str, object],
        decision: TrustDecision,
    ) -> tuple[bool, bool]:
        """Return (granted, needs_human_input).

        granted           — False when blocked or declined, True to proceed.
        needs_human_input — True when a human must answer before execution.
        """
        if decision.kind is TrustDecisionKind.BLOCK:
            return False, False
        if decision.kind is TrustDecisionKind.EXECUTE:
            return True, False

        # CONFIRM path.
        if self.requester is None:
            log.info("Confirmation required for %s but no requester attached — blocking.", spec.name)
            return False, True

        question = self._question(spec, args)
        if self.speak is not None:
            try:
                self.speak(question)
            except Exception:  # noqa: BLE001
                pass
        try:
            reply = self.requester.ask(question)
        except Exception:  # noqa: BLE001
            reply = ""
        granted = parse_confirmation(reply)
        log.info("Confirmation for %s -> granted=%s", spec.name, granted)
        return granted, False

    @staticmethod
    def _question(spec: CapabilitySpec, args: Dict[str, object]) -> str:
        action = args.get("action")
        what = f", action '{action}'" if action else ""
        return f"Should I run '{spec.name}'{what}? This is rated {spec.risk_for_action(action).value} risk."