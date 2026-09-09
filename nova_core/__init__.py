"""nova_core — NOVA's runtime spine.

The audit found that NOVA's problem was not missing components but
disconnected ones: a sound event bus, a capability registry and a
verification engine all existed and were reachable from nothing. This package
is the seam they plug into, plus the one piece that genuinely did not exist
(authorisation).

Deliberately thin. It owns no models, no UI and no tools -- only the
decisions every subsystem needs to share.
"""
from __future__ import annotations

__all__ = ["permissions"]
