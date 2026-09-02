"""
NOVA Trust/Risk — confirmation policy and learned preferences.

The trust engine sits between intent resolution and capability execution:

    Intent → Capability → Confidence → Risk → Permission →
    Confirmation Policy → Execution

Guarantees:
- CRITICAL and HIGH risk actions always require explicit user confirmation.
- Learned preferences can never lower the confirmation requirement for
  CRITICAL (or HIGH) risk actions.
- Without a confirmation UI attached, an unanswered CONFIRM decision
  blocks execution (silent-decline, never silent-approve).
"""
from trust.confirmation import (
    ConfirmationGate,
    ConfirmationRequester,
    MockConfirmationRequester,
    TextConfirmationRequester,
)
from trust.trust_engine import (
    PreferenceStore,
    TrustDecision,
    TrustDecisionKind,
    TrustEngine,
)

__all__ = [
    "ConfirmationGate",
    "ConfirmationRequester",
    "MockConfirmationRequester",
    "TextConfirmationRequester",
    "PreferenceStore",
    "TrustDecision",
    "TrustDecisionKind",
    "TrustEngine",
]