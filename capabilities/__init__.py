"""
NOVA Capabilities — Unified capability contract and platform capability registry.

This package is the canonical single source for capability metadata
(risk, confirmation, platform, permission, online requirement) and
provides a uniform dispatch path that replaces the duplicated
``core/capability_bus.py``, ``agent/tools/registry.py`` and
``nova.py:TOOL_DECLARATIONS`` metadata surfaces.

The registry stays additive and non-destructive: it can be built with a
fallback dispatcher (e.g. ``nova._execute_tool_sync``) so capabilities the
registry does not natively own still route through the existing runtime.
"""
from capabilities.contracts import (
    CapabilityContext,
    CapabilitySpec,
    CapabilityStatus,
    ConfirmationPolicy,
    RiskLevel,
)
from capabilities.registry import (
    CapabilityNotFoundError,
    CapabilityRegistry,
    build_core_registry,
)

__all__ = [
    "CapabilityContext",
    "CapabilitySpec",
    "CapabilityStatus",
    "ConfirmationPolicy",
    "RiskLevel",
    "CapabilityNotFoundError",
    "CapabilityRegistry",
    "build_core_registry",
]