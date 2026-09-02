"""
desk — NOVA Desktop application layer.
======================================

A thin application shell around the EXISTING NOVA intelligence stack. It does
NOT re-implement the brain: it reuses nova.py's pipeline in-process
(_call_gemini_chat / _execute_tool_sync / build_memory_context / _gemini_vision
/ listen_offline-style capture) and exposes it through a polished local API.

Run via:  python nova.py --desk    (opens the WebView2 desktop window)
Headless: NOVA_DESK_HEADLESS=1 python nova.py --desk   (API only, no window)

Layout
------
desk.bridge       Flask app + all /api endpoints + static SPA host
desk.chat         turn router (streaming Gemini chat + tool loop + fallbacks)
desk.confirm      UI-driven safety confirmation (monkeypatches nova_safety)
desk.voice        push-to-talk microphone capture + transcription
desk.store        SQLite conversation persistence
desk.settings     user settings on disk (%APPDATA%/NOVA or ~/.nova)
desk.static       the SPA frontend served at /
"""

APP_NAME = "NOVA"
DESK_DEFAULT_PORT = 8765

__all__ = ["run_desk_server"]