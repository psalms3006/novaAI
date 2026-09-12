"""
nova_safety.py — Tier 6 safety rails for Project NOVA
═══════════════════════════════════════════════════════
Adds:
  - Hard programmatic confirmation gate before consequential tools execute
  - Prompt injection detection in tool results / user input
  - Per-action (not per-session) confirmation — no blanket approval
  - Accessible audit trail via /audit command
  - Cost counter (token usage estimate)

HOW TO WIRE IN (two changes to nova.py):

1. In _execute_tool_sync(), before the main dispatch logic, add:
    from nova_safety import safety_gate, log_tool_run
    gate_result = safety_gate(tool_name, args, get_confirmation_fn)
    if gate_result is not None:
        return gate_result  # user declined or gate blocked
    ...existing dispatch...
    log_tool_run(tool_name, args, result)
    return result

2. In _receive_audio() / offline loop, add injection check:
    from nova_safety import check_injection
    if check_injection(user_input):
        print("[NOVA] ⚠️  Possible prompt injection in input — flagging.")
        # proceed with caution or ask user

For the /audit REPL command:
    if user_input == "/audit":
        from nova_safety import get_audit_log
        speak(get_audit_log())
        continue
"""

from __future__ import annotations
import json
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

# ── Runtime data location ────────────────────────────────────────────────────
# Relative paths resolve against the working directory, which for the packaged
# app is the install directory — not writable under Program Files for a standard
# user, and contrary to the installer's guarantee that user data never lives
# inside the install directory.

def _runtime_data_dir() -> Path:
    import os
    import sys
    if getattr(sys, "frozen", False):
        base = os.getenv("APPDATA")
        d = Path(base) / "NOVA" if base else Path.home() / ".nova"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            return Path(".")
        return d
    return Path(".")


# ── Config (can be overridden by nova_config.toml) ───────────────────────────
AUDIT_FILE       = _runtime_data_dir() / "nova_audit.log"
AUDIT_MAX_LINES  = 500   # rotate after this many entries
COST_FILE        = _runtime_data_dir() / "nova_cost.json"
COST_PER_1K_IN  = 0.0   # Gemini Flash free tier
COST_PER_1K_OUT = 0.0   # update if on paid tier

try:
    import tomllib
    _cfg = tomllib.loads(Path("nova_config.toml").read_text())
    AUDIT_MAX_LINES = _cfg.get("safety", {}).get("audit_max_lines", AUDIT_MAX_LINES)
except Exception:
    pass

# ── Tools that MUST get explicit confirmation before running ──────────────────
CONSEQUENTIAL_TOOLS: Dict[str, str] = {
    # tool_name → human-readable description of what it does
    "self_editor":      "modify NOVA's own source code",
    "send_message":     "send a message on your behalf",
    "file_controller":  "delete/move/write files on disk",   # only destructive actions
    "computer_settings":"change system settings",
    "autostart":        "change Windows startup behaviour",
    "game_updater":     "install or update games",
    "desktop_control":  "change desktop wallpaper or organise files",
}

# Actions within tools that are safe without confirmation
#: Fallback allowlist, used only when the permission engine cannot be
#: imported. nova_core.permissions is the source of truth; anything added
#: here must be added there too, or the two will disagree again.
SAFE_ACTIONS: Dict[str, set] = {
    "file_controller":  {"list", "read", "find", "info"},
    "computer_settings":{"screenshot"},
    "autostart":        {"status"},
    "game_updater":     {"list", "download_status"},
    "self_editor":      {"read", "list_backups"},
}

# ── Injection detection keywords ──────────────────────────────────────────────
_INJECTION_PATTERNS = [
    "ignore your", "ignore previous", "disregard your", "forget your",
    "new instructions:", "system prompt:", "you are now", "act as",
    "bypass your", "override your", "your rules", "don't follow",
    "pretend you", "roleplay as", "jailbreak", "dan mode",
]

# ── Thread-safe audit log ─────────────────────────────────────────────────────
_audit_lock = threading.Lock()
_audit_entries: List[str] = []


def _log(entry: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {entry}"
    with _audit_lock:
        _audit_entries.append(line)
        if len(_audit_entries) > AUDIT_MAX_LINES:
            _audit_entries.pop(0)
        try:
            with AUDIT_FILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


# ── Confirmation gate ─────────────────────────────────────────────────────────

def safety_gate(
    tool_name: str,
    args: dict,
    get_confirmation: Callable[[], str],
    speak_fn: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """
    Returns:
      - None  → safe to proceed, run the tool
      - str   → blocked; return this string to the model instead of running

    get_confirmation must return the user's text response (blocking call).
    Each consequential action asks independently — no blanket pre-approval.
    """
    if tool_name not in CONSEQUENTIAL_TOOLS:
        return None  # unrestricted — go ahead

    action = args.get("action", "")

    # Ask the permission engine, which is the one place that decides this.
    #
    # SAFE_ACTIONS below is a second allowlist answering the same question,
    # and two lists drift: this one named only "screenshot" as safe for
    # computer_settings, so NOVA stopped to ask "should I proceed?" before
    # reading a battery percentage — out loud, mid-conversation — while the
    # permission engine had already decided that was fine. Defer to the
    # engine; keep the list below only for when it cannot be reached.
    try:
        from nova_core import permissions as _perm
        decision = _perm.check_tool("nova", tool_name, args=args)
        if decision.effect is _perm.Effect.ALLOW:
            return None
        if decision.effect is _perm.Effect.DENY:
            return f"Refused: {decision.reason}."
    except Exception:
        pass                      # fall back to the static list below

    safe_for_this_tool = SAFE_ACTIONS.get(tool_name, set())
    if action in safe_for_this_tool:
        return None  # read-only variant — go ahead

    description = CONSEQUENTIAL_TOOLS[tool_name]
    what = _describe_action(tool_name, args)

    prompt = (
        f"I'm about to {description}. "
        f"Specifically: {what}. "
        f"Should I proceed? (yes/no)"
    )

    if speak_fn:
        try:
            speak_fn(prompt)
        except Exception:
            pass
    else:
        print(f"\n🔐 NOVA: {prompt}")

    _log(f"GATE: awaiting confirmation for {tool_name}({json.dumps(args)[:80]})")

    try:
        response = get_confirmation()
    except Exception:
        response = ""

    confirmed = bool(response) and any(
        w in response.lower()
        for w in ["yes", "yeah", "sure", "ok", "okay", "go", "proceed", "do it", "confirm"]
    )

    if confirmed:
        _log(f"CONFIRMED: {tool_name}")
        return None  # proceed
    else:
        _log(f"DECLINED: {tool_name} — user said: {response[:40]!r}")
        return f"Action cancelled. You said: '{response}'. I won't {description} without your explicit yes."


def _describe_action(tool_name: str, args: dict) -> str:
    """Human-readable summary of what the tool will do."""
    action = args.get("action", "")
    if tool_name == "self_editor":
        if action == "patch":
            old = (args.get("old_code") or "")[:60]
            return f"replace code block: '{old}...'"
        if action == "restart":
            return "restart NOVA"
        return f"self_editor/{action}"
    if tool_name == "send_message":
        recv = args.get("receiver", "?")
        plat = args.get("platform", "?")
        text = (args.get("message_text") or "")[:60]
        return f"send '{text}...' to {recv} on {plat}"
    if tool_name == "file_controller":
        path = args.get("path", "?")
        return f"{action} on {path}"
    return f"{tool_name}/{action} with {json.dumps(args)[:60]}"


# ── Tool run logger ───────────────────────────────────────────────────────────

def log_tool_run(tool_name: str, args: dict, result: str):
    preview = str(result)[:100].replace("\n", " ")
    _log(f"TOOL: {tool_name}({json.dumps(args)[:60]}) → {preview}")


# ── Prompt injection detector ─────────────────────────────────────────────────

def check_injection(text: str) -> bool:
    """
    Returns True if the text looks like it's trying to hijack NOVA's instructions.
    Call on user input AND on tool results fetched from the internet.
    """
    lower = text.lower()
    for pattern in _INJECTION_PATTERNS:
        if pattern in lower:
            _log(f"INJECTION_SUSPECTED: pattern='{pattern}' in: {text[:120]!r}")
            return True
    return False


def flag_injection(text: str, speak_fn: Optional[Callable[[str], None]] = None) -> str:
    """Flag injection attempt and return a safe reply."""
    msg = (
        "I noticed that content appears to contain instructions trying to change "
        "my behaviour. I'm treating it as data only, not as a command. "
        "Just so you know — I spotted it."
    )
    if speak_fn:
        try:
            speak_fn(msg)
        except Exception:
            pass
    _log(f"INJECTION_FLAGGED: {text[:80]!r}")
    return msg


# ── Audit log accessor ────────────────────────────────────────────────────────

def get_audit_log(last_n: int = 20) -> str:
    with _audit_lock:
        entries = _audit_entries[-last_n:]
    if not entries:
        return "Audit log is empty."
    return "\n".join(entries)


# ── Cost counter ──────────────────────────────────────────────────────────────

_cost_lock = threading.Lock()
_session_tokens = {"in": 0, "out": 0}


def track_tokens(in_tokens: int, out_tokens: int):
    with _cost_lock:
        _session_tokens["in"]  += in_tokens
        _session_tokens["out"] += out_tokens
    # persist
    try:
        prev = {}
        if COST_FILE.exists():
            prev = json.loads(COST_FILE.read_text())
        prev["total_in"]  = prev.get("total_in",  0) + in_tokens
        prev["total_out"] = prev.get("total_out", 0) + out_tokens
        prev["sessions"]  = prev.get("sessions",  0) + (1 if in_tokens == 0 else 0)
        COST_FILE.write_text(json.dumps(prev, indent=2))
    except Exception:
        pass


def get_cost_summary() -> str:
    with _cost_lock:
        s_in  = _session_tokens["in"]
        s_out = _session_tokens["out"]
    total_cost = (s_in / 1000 * COST_PER_1K_IN) + (s_out / 1000 * COST_PER_1K_OUT)
    return (
        f"Session: ~{s_in:,} in / {s_out:,} out tokens | "
        f"Est. cost: ${total_cost:.4f}"
    )
