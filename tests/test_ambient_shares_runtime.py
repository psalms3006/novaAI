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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "desk" / "static" / "app.js").read_text(encoding="utf-8")


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


def test_playback_is_owned_by_the_backend_not_the_windows():
    """Two surfaces playing the same PCM is the same audio twice."""
    assert 'case "audio":' in APP_JS
    audio_case = APP_JS.split('case "audio":')[1].split("break;")[0]
    assert "playAudioChunk" not in audio_case, (
        "a browser surface plays audio again; with the full window and the "
        "ambient orb both open that is NOVA speaking twice")


def test_no_window_stops_the_session_when_it_is_hidden_or_closed():
    """Switching modes hides a window. Hiding must not end the conversation."""
    for event in ("beforeunload", "pagehide", "visibilitychange", "unload"):
        assert event not in APP_JS, (
            f"a {event} handler exists; switching modes or minimising would "
            "tear down the shared voice session")


def test_the_session_is_only_stopped_by_a_deliberate_act():
    """Stopping ends voice for *every* surface, so it must be explicit.

    Detaching a single surface is a different act with its own function.
    Conflating the two means one window failing to start voice tears down a
    conversation happening in another.
    """
    calls = [m for m in re.finditer(r"^\s*(?:await\s+)?liveDisconnect\(\)",
                                    APP_JS, re.MULTILINE)]
    assert calls, "liveDisconnect is gone; check this test still means anything"
    for m in calls:
        window = APP_JS[max(0, m.start() - 400):m.start()]
        assert "addEventListener" in window and "click" in window, (
            "liveDisconnect is called from something other than a user "
            "action; mode switching or a failed start must not reach it")


def test_a_failed_start_detaches_without_ending_the_conversation():
    assert "function liveDetach" in APP_JS, "no way to leave without stopping"
    body = APP_JS[APP_JS.index("async function liveStart"):]
    body = body[:body.index("\n}")]
    assert "liveDetach()" in body
    assert "liveDisconnect()" not in body


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


def test_the_microphone_is_opened_once_by_the_backend():
    """Browser-side capture would mean one microphone per open window."""
    assert "getUserMedia" not in APP_JS, (
        "a surface captures the microphone itself; two windows would mean two "
        "microphones feeding one conversation")
