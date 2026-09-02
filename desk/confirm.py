"""desk.confirm — UI-driven safety confirmation for consequential tools.

nova.py's _execute_tool_sync imports `safety_gate` from nova_safety *inside the
function body on every call*, so patching nova_safety.safety_gate at runtime is
sufficient to route every tool confirmation through the desktop UI instead of
the blocking stdin prompt. Zero nova.py edits required.

The original gate logic is preserved (same consequential tools / safe actions /
audit logging); only the "ask the user" mechanism is replaced by a store the
frontend polls and answers.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Optional

try:
    import nova_safety as _safety
    from nova_safety import (
        CONSEQUENTIAL_TOOLS,
        SAFE_ACTIONS,
        _describe_action,
        _log,
    )
    _HAS_SAFETY = True
except Exception:  # pragma: no cover - nova_safety is always present in NOVA
    _HAS_SAFETY = False


class ConfirmationStore:
    """Holds pending confirmation requests; frontend polls + answers them."""

    def __init__(self):
        self._pending: dict = {}
        self._lock = threading.Lock()
        self.timeout = 300.0

    # ----- write side (backend / gate) --------------------------------------
    def ask(self, tool_name: str, args: dict, prompt: str, timeout: float | None = None):
        """Block until the UI answers. Returns (decided_yes: bool|None, note: str)."""
        req_id = uuid.uuid4().hex
        evt = threading.Event()
        with self._lock:
            self._pending[req_id] = {
                "id": req_id,
                "tool": tool_name,
                "args": args,
                "prompt": prompt,
                "created": time.time(),
                "decision": None,
                "note": "",
                "event": evt,
            }
        timeout = timeout if timeout is not None else self.timeout
        evt.wait(timeout=timeout)
        with self._lock:
            rec = self._pending.pop(req_id, None)
        if rec is None or rec["decision"] is None:
            return None, "No confirmation received in time; action cancelled."
        return rec["decision"], rec["note"]

    # ----- UI side (frontend) ------------------------------------------------
    def pending(self, limit: int = 20) -> list:
        now = time.time()
        out = []
        with self._lock:
            for rec in self._pending.values():
                if now - rec["created"] > self.timeout:
                    rec["event"].set()  # unblock a stale waiter
                    continue
                out.append({
                    "id": rec["id"],
                    "tool": rec["tool"],
                    "prompt": rec["prompt"],
                    "created": rec["created"],
                })
                if len(out) >= limit:
                    break
        return out

    def decide(self, req_id: str, yes: bool, note: str = "") -> bool:
        with self._lock:
            rec = self._pending.get(req_id)
            if rec is None:
                return False
            rec["decision"] = yes
            rec["note"] = note
            rec["event"].set()
        return True

    def clear(self):
        with self._lock:
            for rec in self._pending.values():
                rec["decision"] = None
                rec["event"].set()
            self._pending.clear()


store = ConfirmationStore()

# Mapping of tool names to permission categories (see desk.settings).
TOOL_CATEGORY = {
    "web_search": "web",
    "browser_control": "web",
    "fetch_url": "web",
    "vision": "screen",
    "screen": "screen",
    "screen_analysis": "screen",
    "desktop_control": "screen",
    "computer_control": "computer",
    "computer_settings": "computer",
    "open_app": "computer",
    "close_app": "computer",
    "autostart": "computer",
    "game_updater": "computer",
    "wake_detector": "mic",
    "file_controller": "files",
    "file_processor": "files",
    "self_editor": "exec",
    "run": "exec",
    "execute": "exec",
    "run_code": "exec",
    "send_message": "network",
    "remember_fact": "memory",
    "nova_memory": "memory",
}


def _permission_for(tool_name: str) -> str:
    """allow | ask | deny — decided by the per-category permission settings."""
    try:
        from .settings import get as _get_settings
        perms = _get_settings("permissions", {}) or {}
        cat = TOOL_CATEGORY.get(tool_name, "ask")
        return perms.get(cat, "ask")
    except Exception:
        return "ask"


def _ui_safety_gate(tool_name: str, args: dict, get_confirmation=None, speak_fn=None):
    """Drop-in replacement for nova_safety.safety_gate driven by the UI store.

    Permission model:
      * category == "deny"  → the action is blocked outright.
      * category == "allow" → safe actions run freely; consequential actions
                              are auto-approved (the user configured them allowed).
      * category == "ask"   → safe actions run; consequential actions require a
                              UI confirmation.
    """
    if not _HAS_SAFETY:
        return None
    action = args.get("action", "")
    _is_consequential = bool(CONSEQUENTIAL_TOOLS.get(tool_name)) and \
        action not in SAFE_ACTIONS.get(tool_name, set())
    perm = _permission_for(tool_name)
    if perm == "deny":
        _log(f"PERM: denied {tool_name} (category {TOOL_CATEGORY.get(tool_name, '?')} = deny)")
        cat = TOOL_CATEGORY.get(tool_name, "that capability")
        return (f"I can't do that — {cat} access is disabled in your permission "
                f"settings. Enable it to let me help with this.")
    if not _is_consequential:
        return None
    if perm == "allow":
        _log(f"PERM: auto-approved consequential {tool_name} (category allowed)")
        return None
    description = CONSEQUENTIAL_TOOLS[tool_name]
    what = _describe_action(tool_name, args)
    prompt = (
        f"NOVA wants to {description}. Specifically: {what}. "
        f"Should I proceed?"
    )
    _log(f"GATE: awaiting UI confirmation for {tool_name}({str(args)[:80]})")
    decided, note = store.ask(tool_name, args, prompt)
    if decided is True:
        _log(f"CONFIRMED: {tool_name}")
        return None
    _log(f"DECLINED: {tool_name} — {note}")
    return (
        f"Action cancelled. I won't {description} without your explicit yes. "
        f"({note})"
    )


def install(ui_mode: bool = True) -> tuple[bool, str]:
    """Wire the UI gate into nova_safety. Call once at desktop-server startup."""
    if not _HAS_SAFETY:
        return False, "nova_safety unavailable"
    if ui_mode:
        _safety.safety_gate = _ui_safety_gate
        return True, "UI confirmation gate installed"
    return False, "UI confirmation gate disabled"