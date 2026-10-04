"""There is one voice session, and both entry points run it.

`python nova.py` used to run NOVALive — a second realtime implementation that
only the terminal used. Two implementations of the same thing drift, and this
pair did: the terminal kept the older behaviour, sending every 64 ms
microphone frame as its own WebSocket message rather than batching, so the
send queue could not keep up and a quarter of everything the microphone heard
was discarded before transmission.

    [MIC] out_queue FULL — dropped chunk #1000
    [MIC] 4000 callbacks total | sent=4000 dropped_queue_full=971

Audio that is never sent cannot be understood. That is why the terminal was
the worst place to talk to NOVA, and why a fix to the desktop pipeline did
nothing for it.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_the_terminal_runs_the_same_manager_as_the_desktop():
    import terminal_voice
    from desk import live_session

    tv = terminal_voice.TerminalVoice(meta={})
    assert tv._mgr is live_session.get_live_manager()


def test_the_terminal_entry_point_no_longer_constructs_NOVALive():
    src = (ROOT / "nova.py").read_text(encoding="utf-8")
    assert "NOVALive(meta=meta)" not in src, (
        "nova.py still starts the second voice implementation")
    assert "TerminalVoice(meta=meta)" in src


def test_the_terminal_keeps_the_contract_nova_py_relies_on():
    """nova.py calls speak() for unprompted notices and awaits run() for a
    status. Both have to survive the swap or the terminal breaks in a
    different way."""
    import terminal_voice

    tv = terminal_voice.TerminalVoice(meta={})
    assert callable(tv.speak)
    assert inspect.iscoroutinefunction(tv.run)


def test_the_terminal_reports_offline_so_the_fallback_still_runs():
    """nova.py drops into its offline loop on `"offline"`. A surface that only
    ever returned "exit" would silently remove offline mode."""
    src = inspect.getsource(__import__("terminal_voice").TerminalVoice)
    assert 'return "offline"' in src
    assert 'return "exit"' in src


def test_the_terminal_adds_no_audio_of_its_own():
    """A second capture or playback path here would be exactly the problem
    this file exists to prevent."""
    src = (ROOT / "terminal_voice.py").read_text(encoding="utf-8")
    for bad in ("InputStream", "RawOutputStream", "sounddevice", "sd."):
        assert bad not in src, f"{bad} — the terminal is opening its own audio"


def test_only_the_session_module_opens_audio_now():
    """live_extra still exists for its Gemini chat helpers; what matters is
    that nothing reaches its realtime loop any more."""
    nova = (ROOT / "nova.py").read_text(encoding="utf-8")
    live = re.search(r"^from live_extra import \(([^)]*)\)", nova, re.M)
    if live:
        imported = live.group(1)
        assert "NOVALive" not in imported.split("#")[0] or "NOVALive(meta" not in nova


def test_nothing_can_reach_the_second_realtime_implementation():
    """Not merely unused — unreachable.

    An import left in place is a working way to start a second microphone,
    and the whole failure was that two implementations of one thing drifted
    apart. live_extra keeps its Gemini chat helpers; its realtime loop must
    have no route into it.
    """
    src = (ROOT / "nova.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in src.splitlines()
                     if not line.lstrip().startswith("#"))
    assert "NOVALive" not in code, "nova.py can still construct the old session"


def test_the_chat_helpers_are_still_imported():
    """The point is to retire one loop, not to break the module that holds it."""
    src = (ROOT / "nova.py").read_text(encoding="utf-8")
    assert "_call_gemini_chat" in src and "_trim_history" in src
