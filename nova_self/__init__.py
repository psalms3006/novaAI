"""NOVA changing NOVA, under conditions that make it safe to allow.

Propose, rehearse in an isolated checkout, apply only with the owner's word,
verify, and undo automatically when verification fails. The rules about what
may change are themselves in the protected set, so a change cannot quietly
widen its own permission.
"""
from __future__ import annotations

__all__ = ["improve", "journal", "protected"]
