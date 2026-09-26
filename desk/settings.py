"""desk.settings — persistent user settings for the NOVA desktop app.

Stored as JSON in %APPDATA%/NOVA/settings.json (Windows) or ~/.nova/settings.json.
Secrets are NEVER stored here — only UI/user preferences and capability toggles.

Every key in `_DEFAULTS` must control real behaviour somewhere in the stack
(desk backend / SPA / voice pipeline). No decorative/fake toggles.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

_LOCK = threading.Lock()

_PERMISSION_CATEGORIES = [
    "web",        # web search / browsing
    "network",    # outbound network calls
    "screen",     # screen capture & vision analysis
    "mic",        # microphone capture / listening
    "files",      # reading/writing/deleting files
    "computer",   # open/close apps, system settings, automation
    "exec",       # running commands / code
]

_DEFAULTS = {
    # ── identity ───────────────────────────────────────────────
    "user_name": "User",            # neutral default; user may change it
    "user_name_pronunciation": "",  # how to say it, when spelling does not say
    # Who this person is to NOVA, in their own words: "the person who built
    # me", "my colleague", whatever fits. Free text, and it reaches the model
    # verbatim.
    #
    # It exists because semantic recall could not answer it. Memory had
    # accumulated five different answers to "what is the user called" -- a
    # full name, two nicknames, two corrections -- and retrieval returned
    # whichever was closest to the question, so NOVA was inconsistent about a
    # person she had been introduced to many times, and kept asking again.
    # Identity is not a fact to search for; it is a fact to be told.
    "user_role": "",
    "mic_device": "",               # input device name or index; blank = system default
    "voice_language": "en-US",      # BCP-47 hint for speech recognition
    "user_system_prompt": "",       # user's custom NOVA instructions (reaches inference)
    # ── appearance (applied client-side, persisted server-side) ──
    "theme": "dark",                # dark | light
    "accent": "violet",             # violet | blue | teal | amber | rose
    "density": "comfortable",       # comfortable | compact
    "animations": "full",           # full | reduced | off
    # ── intelligence ───────────────────────────────────────────
    "streaming": True,              # SSE streaming vs single completion
    "response_style": "balanced",   # concise | balanced | detailed (enters system prompt)
    "memory_enabled": True,         # gates memory context + auto-extraction
    # How NOVA searches the user's documents. Chosen during setup:
    #   onnx    a small model on this machine (semantic, offline, ~90 MB)
    #   cloud   embeddings from the AI provider (best quality, needs network)
    #   lexical keyword search only (nothing downloaded, nothing sent)
    # "auto" prefers local, then cloud, then keyword. NOVA reports which is
    # actually in use rather than implying semantic search it does not have.
    "embedding_backend": "auto",
    "history_turns": 10,            # turns of context sent to the model
    "show_tool_activity": True,     # render tool activity cards in the thread
    "developer_mode": False,        # gates advanced/debug UI
    # ── voice ──────────────────────────────────────────────────
    "voice_enabled": True,          # gate STT push-to-talk button
    "voice_responses": True,        # NOVA speaks replies (TTS in the client)
    "continuous_conversation": False,  # auto-listen loop after each reply
    "auto_listen": False,           # start listening when idle & enabled
    "barge_in": True,               # interrupt NOVA's speech with new input
    # ── safety / permissions ───────────────────────────────────
    "confirm_policy": "prompt",     # prompt | auto-safe
    "permissions": {                # per-category: allow | ask | deny
        "web": "allow",
        "network": "allow",
        "screen": "ask",
        "mic": "allow",
        "files": "ask",
        "computer": "ask",
        "exec": "ask",
    },
    # ── general / window ───────────────────────────────────────
    "launch_on_startup": False,
    "start_minimized": False,
    "remember_window": True,
    "workspace_dir": "",            # where NOVA may create files ("" = default)
    "offline_fallback": True,       # permit offline reasoning path when online is down
    # ── local intelligence ─────────────────────────────────────
    "local_model": "qwen2.5:3b",    # default Ollama model for offline reasoning
    "online_preferred": True,       # prefer online provider when available
    "auto_download_model": False,   # auto-download default model on first run
    "local_model_timeout": 30,      # seconds before local model request times out
    # ── account / credentials ──────────────────────────────────
    # NOTE: no API key is ever stored here. BYOK keys live in the OS-protected
    # credential store (DPAPI); cloud sessions live only in memory.
    "auth_mode": "",                # "" | "cloud" | "byok" | "offline" | "env"
    "cloud_url": "",                # NOVA backend/gateway base URL (empty = disabled)
    "onboarded": False,             # first-run onboarding completed
    # ── interface (desk/ui) ─────────────────────────────────────
    # The window's look: theme, glass, motion, start screen. Kept here, not in
    # the page's localStorage, because the desktop window runs in pywebview's
    # private mode, which wipes browser storage at every launch. Merged key
    # by key and validated against _UI_PREFS below.
    "ui_prefs": {},
}

# Every interface preference the window may store, and what counts as valid.
# A tuple is the allowed set; a (min, max) pair of numbers is an inclusive
# range; `bool` is a flag. Anything else in an update is dropped.
_UI_PREFS = {
    "theme_id": ("obsidian", "titanium", "warm-graphite", "deep-ocean", "pearl"),
    "glass_opacity": (30, 95),
    "glass_blur": (8, 40),
    "specular": (0, 100),
    "ui_scale": ("90", "100", "110"),
    "corner_radius": ("sharp", "soft", "round"),
    "ambient_motion": bool,
    "quality": ("quality", "balanced", "performance"),
    "landing_view": ("substrate", "synaptic", "runtime"),
    "time_format": ("12h", "24h"),
    "presence": ("orb", "humanoid"),
    "show_transcript": bool,
}


def _clean_ui_prefs(update: dict) -> dict:
    """The valid subset of an interface-preferences update."""
    clean = {}
    for key, value in update.items():
        rule = _UI_PREFS.get(key)
        if rule is None:
            continue
        if rule is bool:
            if isinstance(value, bool):
                clean[key] = value
        # (This module's own all() shadows the builtin, hence no all() here.)
        elif (isinstance(rule, tuple) and len(rule) == 2
              and isinstance(rule[0], (int, float)) and isinstance(rule[1], (int, float))):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if rule[0] <= value <= rule[1]:
                    clean[key] = value
        elif value in rule:
            clean[key] = value
    return clean

# Keys the SPA is allowed to persist (everything user-facint is persisted server-side;
# nothing here is a secret).
_SAFE_KEYS = set(_DEFAULTS.keys())


def app_data_dir() -> Path:
    base = os.getenv("APPDATA") or ""
    if base:
        d = Path(base) / "NOVA"
    else:
        d = Path.home() / ".nova"
    d.mkdir(parents=True, exist_ok=True)
    return d


def settings_path() -> Path:
    return app_data_dir() / "settings.json"


def _load() -> dict:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def get(key: str, default=None):
    with _LOCK:
        data = _load()
        return data.get(key, _DEFAULTS.get(key, default))


def set_many(updates: dict) -> dict:
    with _LOCK:
        data = _load()
        for k, v in updates.items():
            if k == "permissions" and isinstance(v, dict):
                # From `data`, never get(): get() takes _LOCK, which this
                # function already holds, and threading.Lock is not
                # re-entrant -- saving a permission deadlocked every later
                # settings read and write in the process.
                current = data.get("permissions")
                merged = dict(_DEFAULTS["permissions"])
                if isinstance(current, dict):
                    merged.update(current)
                for cat, val in v.items():
                    if cat in _PERMISSION_CATEGORIES and val in ("allow", "ask", "deny"):
                        merged[cat] = val
                data["permissions"] = merged
            elif k == "ui_prefs":
                if isinstance(v, dict):
                    current = data.get("ui_prefs")
                    merged = dict(current) if isinstance(current, dict) else {}
                    merged.update(_clean_ui_prefs(v))
                    data["ui_prefs"] = merged
            elif k in _SAFE_KEYS:
                data[k] = v
        try:
            settings_path().write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        return data


def all() -> dict:
    with _LOCK:
        data = _load()
        merged = dict(_DEFAULTS)
        merged.update(data)
        if not isinstance(merged.get("permissions"), dict):
            merged["permissions"] = dict(_DEFAULTS["permissions"])
        prefs = merged.get("ui_prefs")
        merged["ui_prefs"] = _clean_ui_prefs(prefs) if isinstance(prefs, dict) else {}
        return merged


def toggles() -> dict:
    """Which UI toggles actually do something (never advertise fake controls)."""
    return {
        "theme": True,               # applied client-side, persisted here
        "accent": True,              # applied client-side
        "density": True,             # applied client-side
        "animations": True,          # applied client-side
        "memory_enabled": True,      # gates memory context + auto-extraction
        "voice_enabled": True,       # gates the push-to-talk + voice loop
        "voice_responses": True,     # gates spoken replies (client TTS)
        "continuous_conversation": True,  # drives the auto-listen loop
        "auto_listen": True,         # drives idle listening in voice mode
        "barge_in": True,            # cancels TTS on new input
        "streaming": True,           # SSE vs single completion
        "response_style": True,      # enters the effective system prompt
        "show_tool_activity": True,  # renders/hides activity cards
        "developer_mode": True,      # gates the advanced settings section
        "confirm_policy": True,      # prompt vs auto-safe confirmation
        "permissions": True,         # per-category allow/ask/deny (safety gate)
        "ui_prefs": True,            # desk/ui theme, glass, motion, start screen
    }