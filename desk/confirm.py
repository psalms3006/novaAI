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
        try:
            from nova_core import cancel as _cancel
        except Exception:
            _cancel = None
        deadline = time.time() + timeout
        withdrawn = False
        while not evt.wait(timeout=0.25):
            if _cancel is not None and _cancel.cancelled():
                withdrawn = True            # the request was withdrawn: stop asking
                break
            if time.time() >= deadline:
                break
        with self._lock:
            rec = self._pending.pop(req_id, None)
        if withdrawn:
            return False, "The request was withdrawn before you answered."
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

# Which permission scope a tool call needs (see desk.settings). Most tools
# need one scope; for files and the browser it depends on the action, because
# reading and changing are separate decisions.
TOOL_CATEGORY = {
    "web_search": "browser_read",
    "fetch_url": "browser_read",
    "browser_control": "browser_read",
    "vision": "screen_read",
    "screen": "screen_read",
    "screen_analysis": "screen_read",
    "desktop_control": "computer_control",
    "computer_control": "computer_control",
    "computer_settings": "computer_control",
    "open_app": "computer_control",
    "close_app": "computer_control",
    "autostart": "computer_control",
    "game_updater": "computer_control",
    "wake_detector": "microphone",
    "file_controller": "file_read",
    "file_processor": "file_read",
    "self_editor": "exec",
    "run": "exec",
    "execute": "exec",
    "run_code": "exec",
    "send_message": "network",
    # Tools that act without a scope used to run unasked whatever the person
    # chose (the settings screen even showed them as "ask").
    "generate_document": "file_write",
    "app_control": "computer_control",
    "learn_resource": "exec",
}

#: Actions that cannot be taken back. They are asked about every time, even
#: when their permission scope is set to Allow: "Allow computer control" means
#: NOVA may open apps and change the volume unasked, not that she may switch
#: the computer off. On 2026-09-30 a shutdown ran on Allow without a question.
IRREVERSIBLE = {
    "computer_settings": {"shutdown", "restart", "reboot", "sleep", "hibernate", "logoff",
                          "log_off", "sign_out", "signout", "uninstall_app"},
    "computer_control": {"shutdown", "restart", "reboot", "sleep", "hibernate", "logoff",
                         "log_off", "sign_out", "signout"},
    "desktop_control": {"shutdown", "restart", "sleep", "logoff"},
    "file_controller": {"delete", "remove", "rmdir", "delete_folder", "empty_trash"},
    "self_editor": {"apply"},
}


def irreversible(tool_name: str, args: dict | None) -> bool:
    action = str((args or {}).get("action") or "").strip().lower()
    return action in IRREVERSIBLE.get(tool_name, set())


# Tools whose memory of the person is NOVA's own business: not a permission.
_UNGATED = {"remember_fact", "nova_memory", "nova_learning", "nova_capability"}

_FILE_READ_ACTIONS = {"list", "read", "find", "info", "search", "exists", "stat",
                      "summarize", "summarise", "extract", "open"}
_BROWSER_INTERACT_ACTIONS = {"close_tab", "download"}


def scope_for(tool_name: str, args: dict | None = None) -> str:
    """The permission scope this particular call needs ('' = none)."""
    args = args or {}
    action = str(args.get("action") or "").strip().lower()
    if tool_name in _UNGATED and tool_name not in ("nova_learning", "nova_capability"):
        return ""
    if tool_name in ("file_controller", "file_processor"):
        return "file_read" if (action in _FILE_READ_ACTIONS or not action) else "file_write"
    if tool_name == "nova_learning":
        # Studying a folder reads the person's files; asking what was learned does not.
        return "file_read" if str(args.get("cmd") or "").lower() == "learn" else ""
    if tool_name == "nova_capability":
        # Running or testing a skill can send the person's content to an
        # outside service; discovering one only searches the web.
        cmd = str(args.get("cmd") or "").lower()
        if cmd in ("run", "test", "adopt", "propose", "check"):
            return "network"
        return "browser_read" if cmd in ("discover", "read") else ""
    if tool_name.startswith("mcp__") or tool_name in ("use_tool",):
        return "network"               # a connected service, outside NOVA
    if tool_name == "browser_control":
        return "browser_interact" if action in _BROWSER_INTERACT_ACTIONS else "browser_read"
    return TOOL_CATEGORY.get(tool_name, "")


def tool_scopes() -> dict:
    """tool -> every scope it can need, for the settings screen."""
    out = {t: [s] for t, s in TOOL_CATEGORY.items()}
    out["file_controller"] = out["file_processor"] = ["file_read", "file_write"]
    out["browser_control"] = ["browser_read", "browser_interact"]
    out["nova_learning"] = ["file_read"]
    out["nova_capability"] = ["browser_read", "network"]
    return out


_SCOPE_WORDS = {
    "microphone": "microphone", "screen_read": "screen", "file_read": "reading files",
    "file_write": "changing files", "browser_read": "web", "browser_interact": "browser control",
    "computer_control": "computer control", "exec": "running programs", "network": "network",
}


def _permission_for(tool_name: str, args: dict | None = None) -> str:
    """allow | ask | deny — decided by the per-scope permission settings."""
    scope = scope_for(tool_name, args)
    if not scope:
        return "allow" if tool_name in _UNGATED else "ask"
    try:
        from .settings import get as _get_settings
        perms = _get_settings("permissions", {}) or {}
        return perms.get(scope, "ask")
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
    perm = _permission_for(tool_name, args)
    scope = scope_for(tool_name, args)
    if perm == "deny":
        _log(f"PERM: denied {tool_name} (scope {scope or '?'} = deny)")
        what = _SCOPE_WORDS.get(scope, "that")
        return (f"I can't do that — {what} is turned off in your permission "
                f"settings. Turn it on to let me help with this.")
    if perm == "allow" and not irreversible(tool_name, args):
        if _is_consequential:
            _log(f"PERM: auto-approved consequential {tool_name} (scope {scope} allowed)")
        return None
    if not _is_consequential and not scope:
        return None
    # "Ask me" means ask. With reading and changing now separate choices, a
    # person who set "Read your files: Ask me" expects to be asked before a
    # read, not only before the consequential actions the old gate covered.
    description = CONSEQUENTIAL_TOOLS.get(tool_name) or         f"use {_SCOPE_WORDS.get(scope, tool_name)} ({tool_name})"
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