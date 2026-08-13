"""
Verification Engine — distinguishes tool success from actual goal outcome.

Rule: a tool returning OK is not proof that the goal step achieved its intent.
This module provides lightweight verification heuristics and a pluggable hook.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from core.capability_bus import Capability

log = logging.getLogger("nova.verification")


@dataclass
class Verification:
    status: str
    reason: str
    confidence: float = 0.0
    metadata: dict = None

    def __post_init__(self) -> None:
        if self.metadata is None:
            self.metadata = {}


class VerificationEngine:
    """
    Verify step results using capability hints and lightweight heuristics.

    Statuses:
    - CONFIRMED_SUCCESS
    - LIKELY_SUCCESS
    - FAILURE
    - UNKNOWN
    """

    def verify(
        self,
        step: Any,
        raw_output: Optional[str],
        capability: Optional[Capability] = None,
    ) -> Verification:
        text = (raw_output or "").strip()

        if not text:
            return Verification("FAILURE", "empty result", 0.0)

        lower = text.lower()
        failure_signals = [
            "error:", "failed", "exception", "not found", "unavailable",
            "denied", "blocked", "timeout", "unreachable", "unable to",
        ]
        for signal in failure_signals:
            if signal in lower:
                return Verification("FAILURE", f"failure signal: {signal}", 0.2)

        success_signals = [
            "saved:", "created:", "submitted:", "completed:", "ready:",
            "✅", "remembered:", "applied", "results:", "found ",
        ]
        for signal in success_signals:
            if signal in lower:
                return Verification("CONFIRMED_SUCCESS", f"success signal: {signal}", 0.8)

        hint = capability.verification_hint if capability else None
        if hint and hint.lower().split()[0] in lower.split()[:10]:
            return Verification("LIKELY_SUCCESS", f"matches hint: {hint}", 0.55)

        return Verification("UNKNOWN", "no strong signal", 0.3)
