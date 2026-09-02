"""
Capability contract types for NOVA.

A *capability* is a named, schematized device/system operation (e.g.
``location.get``, ``contacts.search``, ``web_search``) described by
metadata the trust engine, orchestrator and planner all consume.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class RiskLevel(Enum):
    """Consequence tiers used by the trust/risk engine.

    LOW      — web search, opening apps, safe queries, reading permitted data,
               setting alarms, creating reminders.
    MEDIUM   — calls, ordinary messages, calendar modifications, information
               sharing.
    HIGH     — deleting files, changing important settings, sharing sensitive
               information, destructive actions.
    CRITICAL — financial transactions, credential/security changes,
               irreversible destructive actions.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    def __le__(self, other: "RiskLevel") -> bool:
        order = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]
        return order.index(self) <= order.index(other)


class ConfirmationPolicy(Enum):
    """How confirmation is requested for a capability.

    AUTO   — the trust engine decides from risk, confidence and ambiguity.
    ALWAYS — always ask the user.
    NEVER  — never ask (safe, reversible, read-only).
    """

    AUTO = "auto"
    ALWAYS = "always"
    NEVER = "never"


class CapabilityStatus(Enum):
    """Availability of a capability on the current platform."""
    AVAILABLE = "available"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


# Callable contract: (args, ctx) -> string result (natural-language).
CapabilityHandler = Callable[[Dict[str, Any], "CapabilityContext"], str]


@dataclass
class CapabilitySpec:
    """Static contract for a single device/system capability."""

    name: str
    description: str
    handler: Optional[CapabilityHandler] = None
    # JSON Schema for parameters (Gemini/OpenAI compatible properties).
    parameters: Dict[str, Any] = field(default_factory=dict)
    platforms: List[str] = field(
        default_factory=lambda: ["windows", "linux", "macos", "android", "ios"]
    )
    permissions: List[str] = field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    confirmation_policy: ConfirmationPolicy = ConfirmationPolicy.AUTO
    requires_internet: bool = False
    reversible: bool = False
    verification_hint: str = ""
    # Per-action risk/safety refinements (e.g. file_controller: delete is HIGH).
    action_risk_overrides: Dict[str, RiskLevel] = field(default_factory=dict)
    safe_actions: List[str] = field(default_factory=list)
    # Legacy compatibility fields (kept for capability_bus/agent consumers).
    requires_confirmation: bool = False
    agent: str = "orchestrator"

    def risk_for_action(self, action: Optional[str]) -> RiskLevel:
        """Effective risk of a capability given a specific action argument."""
        if action and action in self.action_risk_overrides:
            return self.action_risk_overrides[action]
        return self.risk_level

    def action_is_safe(self, action: Optional[str]) -> bool:
        return bool(action and action in self.safe_actions)

    def is_available_on(self, platform: str) -> bool:
        return "all" in self.platforms or platform in self.platforms

    def effective_policy(self, action: Optional[str] = None) -> ConfirmationPolicy:
        """CRITICAL risk can never be downgraded below ALWAYS confirmation."""
        if self.risk_for_action(action) is RiskLevel.CRITICAL:
            return ConfirmationPolicy.ALWAYS
        return self.confirmation_policy

    def to_metadata(self) -> Dict[str, Any]:
        """Normalized metadata mapping (aims to match capability_bus shape)."""
        return {
            "name": self.name,
            "description": self.description,
            "agent": self.agent,
            "risk_level": self.risk_level.value,
            "confirmation_policy": self.confirmation_policy.value,
            "requires_confirmation": self.requires_confirmation,
            "reversible": self.reversible,
            "verification_hint": self.verification_hint,
            "platforms": list(self.platforms),
            "permissions": list(self.permissions),
            "requires_internet": self.requires_internet,
            "action_risk_overrides": {
                k: v.value for k, v in self.action_risk_overrides.items()
            },
            "safe_actions": list(self.safe_actions),
        }


@dataclass
class CapabilityContext:
    """Mutable, per-invocation context handed to capability handlers."""

    platform: str = "windows"
    speak: Optional[Callable[[str], None]] = None
    connectivity: Any = None
    session_facts: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)
    config: Any = None

    @property
    def is_online(self) -> bool:
        if self.connectivity is not None:
            return bool(getattr(self.connectivity, "is_fully_online", True))
        return True