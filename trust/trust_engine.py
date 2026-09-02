"""
Trust engine — decides whether a capability executes, confirms, or blocks.

Inputs
------
- capability spec (risk level + confirmation policy + action overrides)
- confidence (0..1) that the planned intent maps to the right capability
- ambiguity: whether the target is ambiguous (e.g. multiple matching contacts)
- forced confirmation / block overlays

Decision rules (spec §8)
------------------------
CRITICAL   → ALWAYS confirm (learned prefs cannot override)
HIGH       → ALWAYS confirm
MEDIUM     → AUTO: execute only when confidence is high, target unambiguous,
             and a learned preference permits the action; otherwise confirm
LOW        → AUTO/NEVER: execute unless ambiguous+low-confidence; ALWAYS → confirm
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

from capabilities.contracts import CapabilitySpec, ConfirmationPolicy, RiskLevel
from core.utils import atomic_json_write, atomic_json_read

log = logging.getLogger("nova.trust")

_DEFAULT_PREFS_PATH = Path("data") / "trust_preferences.json"


class TrustDecisionKind(Enum):
    EXECUTE = "execute"
    CONFIRM = "confirm"
    BLOCK = "block"


@dataclass
class TrustDecision:
    kind: TrustDecisionKind
    reason: str
    risk_level: RiskLevel
    confidence: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "reason": self.reason,
            "risk_level": self.risk_level.value,
            "confidence": self.confidence,
        }


class PreferenceStore:
    """Persistent, explainable learned preferences for safe actions.

    Only LOW-risk, reversible, non-sensitive actions may be recorded as
    "trusted". CRITICAL/HIGH risk is never recorded.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or _DEFAULT_PREFS_PATH
        self._prefs: Dict[str, bool] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        data = atomic_json_read(self._path, default={})
        if isinstance(data, dict):
            self._prefs = {str(k): bool(v) for k, v in data.items()}

    def _save(self) -> None:
        with self._lock:
            atomic_json_write(self._path, self._prefs)

    def allow(self, capability: str, action: Optional[str] = None) -> None:
        """Record a learned preference to trust this capability/action."""
        key = self._key(capability, action)
        with self._lock:
            self._prefs[key] = True
        self._save()
        log.info("Learned trust for %s", key)

    def allowed(self, capability: str, action: Optional[str] = None) -> bool:
        with self._lock:
            return bool(self._prefs.get(self._key(capability, action)))

    def revoke(self, capability: str, action: Optional[str] = None) -> bool:
        key = self._key(capability, action)
        with self._lock:
            removed = self._prefs.pop(key, None) is not None
        self._save()
        return removed

    def snapshot(self) -> Dict[str, bool]:
        with self._lock:
            return dict(self._prefs)

    @staticmethod
    def _key(capability: str, action: Optional[str]) -> str:
        return f"{capability}:{action}" if action else capability


class TrustEngine:
    """Pure decision logic — no side effects, easy to unit test."""

    def __init__(
        self,
        preferences: Optional[PreferenceStore] = None,
        confidence_high: float = 0.8,
        confidence_low: float = 0.5,
    ) -> None:
        self._prefs = preferences or PreferenceStore()
        self.confidence_high = confidence_high
        self.confidence_low = confidence_low

    @property
    def preferences(self) -> PreferenceStore:
        return self._prefs

    def decide(
        self,
        spec: CapabilitySpec,
        action: Optional[str] = None,
        confidence: float = 0.7,
        ambiguous: bool = False,
        forced: bool = False,
        blocked: bool = False,
    ) -> TrustDecision:
        risk = spec.risk_for_action(action)
        policy = spec.effective_policy(action)

        if blocked:
            return TrustDecision(TrustDecisionKind.BLOCK, "blocked by caller", risk, confidence)
        if forced:
            return TrustDecision(TrustDecisionKind.CONFIRM, "explicit confirmation requested", risk, confidence)

        if risk is RiskLevel.CRITICAL:
            # Never auto — learned preferences cannot override this.
            return TrustDecision(
                TrustDecisionKind.CONFIRM,
                "CRITICAL risk — explicit confirmation always required",
                risk, confidence,
            )
        if risk is RiskLevel.HIGH:
            return TrustDecision(
                TrustDecisionKind.CONFIRM,
                "HIGH risk — explicit confirmation required",
                risk, confidence,
            )

        if policy is ConfirmationPolicy.ALWAYS:
            return TrustDecision(
                TrustDecisionKind.CONFIRM,
                "capability mandates confirmation",
                risk, confidence,
            )
        if policy is ConfirmationPolicy.NEVER:
            return TrustDecision(
                TrustDecisionKind.EXECUTE,
                "policy: never confirm; low risk",
                risk, confidence,
            )

        # AUTO policy for LOW/MEDIUM.
        if risk is RiskLevel.MEDIUM:
            allow = self._prefs.allowed(spec.name, action)
            if confidence >= self.confidence_high and not ambiguous and allow:
                return TrustDecision(
                    TrustDecisionKind.EXECUTE,
                    "MEDIUM risk, high confidence, unambiguous, learned preference",
                    risk, confidence,
                )
            return TrustDecision(
                TrustDecisionKind.CONFIRM,
                "MEDIUM risk — requires confirmation",
                risk, confidence,
            )

        return TrustDecision(
            TrustDecisionKind.EXECUTE,
            "LOW risk — safe to execute",
            risk, confidence,
        )

    def learn_from_confirmation(self, spec: CapabilitySpec, action: Optional[str], confirmed: bool) -> None:
        """Record a learned preference after the user confirms a LOW-risk action.

        Explicitly does nothing for MEDIUM+ risk — see module docs.
        """
        if not confirmed:
            return
        risk = spec.risk_for_action(action)
        if risk is RiskLevel.LOW and spec.reversible and spec.effective_policy(action) is not ConfirmationPolicy.ALWAYS:
            self._prefs.allow(spec.name, action)