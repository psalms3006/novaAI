"""There is one microphone, so there is one thing listening through it.

NOVA has two capture paths. The live session holds the microphone open for
the whole conversation; push-to-talk opens one for the length of a recording
and exists for when the live session cannot run at all — no key, no network,
a model that will not connect.

They must never be open at once. On Windows the second open generally
succeeds, and what follows is two readers splitting one input stream with
neither getting a clean signal. That does not present as an error anybody can
see; it presents as NOVA intermittently mishearing, which is the hardest
class of fault to attribute and the one this file exists to prevent.
"""
from __future__ import annotations

import threading

from desk import live_session as ls


def manager(mic_open: bool):
    m = ls.LiveManager.__new__(ls.LiveManager)
    m._mic_active = mic_open
    return m


def test_the_session_reports_whether_it_holds_the_microphone():
    assert manager(True).owns_microphone is True
    assert manager(False).owns_microphone is False


def test_push_to_talk_refuses_while_the_session_is_listening(monkeypatch):
    from desk import bridge

    monkeypatch.setattr(ls, "get_live_manager", lambda: manager(True))

    opened = []
    monkeypatch.setattr(bridge.desk_voice, "start_capture",
                        lambda *a, **k: (opened.append(1), (True, "recording"))[1])
    monkeypatch.setattr(bridge.desk_voice, "status", lambda: {})

    client = bridge.app.test_client()
    r = client.post("/api/voice/start",
                    headers={"X-NOVA-Desk": bridge.run_token})
    body = r.get_json()

    assert body["ok"] is False
    assert body["reason"] == "live_session_owns_microphone"
    assert not opened, "a second capture stream was opened on the same device"


def test_push_to_talk_still_works_when_the_session_is_not_listening(monkeypatch):
    """The fallback has to remain a fallback, not become unreachable."""
    from desk import bridge

    monkeypatch.setattr(ls, "get_live_manager", lambda: manager(False))

    opened = []
    monkeypatch.setattr(bridge.desk_voice, "start_capture",
                        lambda *a, **k: (opened.append(1), (True, "recording"))[1])
    monkeypatch.setattr(bridge.desk_voice, "status", lambda: {})

    client = bridge.app.test_client()
    r = client.post("/api/voice/start",
                    headers={"X-NOVA-Desk": bridge.run_token})

    assert r.get_json()["ok"] is True
    assert opened == [1]


def test_only_these_two_places_open_a_capture_stream_in_the_desktop_app():
    """A third would reintroduce the problem silently."""
    import re
    from pathlib import Path

    desk = Path(__file__).resolve().parent.parent / "desk"
    openers = sorted(
        f.name for f in desk.glob("*.py")
        if re.search(r"\bsd\.InputStream\(", f.read_text(encoding="utf-8"))
    )
    assert openers == ["live_session.py", "voice.py"], (
        f"another microphone owner appeared in desk/: {openers}")
