"""Ambient mode is a second view of one NOVA, not a second NOVA.

Switching between the full window and the ambient orb changes which interface
is on screen. It must not change the assistant: no second Gemini session, no
second microphone, no second speaker, no restart of a reply that is halfway
through being spoken, no lost conversation.

That property is architectural rather than behavioural — it holds because the
session lives in the backend and the windows only subscribe to it — so these
tests assert the architecture. Each one names the thing that would break the
experience if it were reintroduced.
"""
from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

# The checks on the previous window (desk/static) that lived here moved to
# tests/test_desk_ui_behaviour.py when the interface became desk/ui.

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent


def test_there_is_one_live_manager_for_the_process():
    """A per-window manager would be a second NOVA with its own microphone."""
    from desk import live_session

    a = live_session.get_live_manager()
    b = live_session.get_live_manager()
    assert a is b, "each caller got its own voice session"


def test_the_ambient_window_is_the_same_page_not_another_app():
    desktop = (ROOT / "nova_desktop_app.py").read_text(encoding="utf-8")
    assert "?mode=ambient" in desktop, (
        "the ambient orb is no longer a view of the same interface")


def test_screen_awareness_uses_the_conversation_already_in_progress():
    from desk.live_session import LiveManager

    src = inspect.getsource(LiveManager._video_sender)
    assert "session.send_" in src, (
        "screen frames go somewhere other than the live session")
    assert "genai.Client" not in src, "a second Gemini connection for vision"


def test_starting_voice_twice_does_not_start_it_twice():
    """Both windows run the same boot code and both ask for a session."""
    from desk import live_session

    src = inspect.getsource(live_session.LiveManager.start)
    assert "already running" in src, (
        "a second surface asking for voice would open a second session")


def test_every_surface_sees_the_same_state():
    """Fan-out, not per-surface state: the orb must never disagree with the
    window about whether NOVA is speaking."""
    from desk import live_session

    src = inspect.getsource(live_session.LiveManager._publish)
    assert "self._subscribers" in src
    assert "for q in subs" in src


