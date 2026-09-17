"""What the interface is told NOVA is doing, while she is doing it.

Every surface — the desktop window, the ambient orb, the telemetry panel —
draws itself from the event stream `LiveManager` publishes. So a state NOVA
enters but never announces is a state the user cannot see, and the one that
matters most is speaking: it is the longest-lived state in a conversation and
the one during which the user most needs to know whether to wait or to talk.

The reported symptom was an orb that said "listening" for the entire time NOVA
was audibly talking. app.js has handled `state: "speaking"` since it was
written; nothing ever sent it.
"""
from __future__ import annotations

import queue as q
import threading

import nova_voice
from desk import live_session as ls

CHUNK = b"\x10\x00" * 512


def session():
    """A LiveManager with the audio hardware stubbed out."""
    m = ls.LiveManager.__new__(ls.LiveManager)
    m._play_q = q.Queue(maxsize=200)
    m._subscribers = []
    m._subs_lock = threading.Lock()
    m._speaker_alive = True
    m._last_audio_at = 0.0
    m._play_generation = 0
    m._trace = None
    m._turn_done_flag = False
    m._turn_count = 0
    m._audio_bytes_out = 0
    m._gate = nova_voice.VoiceGate(chunk_samples=1024, on_barge_in=m._barge_in)
    m._stream = None
    m._stream_lock = threading.Lock()
    m._playing_until = 0.0
    m._published = []
    m._publish = lambda ev: m._published.append(ev)
    return m


def states(m):
    return [e.data.get("state") for e in m._published if e.type == "state"]


# ── entering speech ──────────────────────────────────────────────────────────

def test_the_first_chunk_of_a_turn_announces_speaking():
    m = session()
    m._enqueue_audio(CHUNK)
    assert "speaking" in states(m), (
        "NOVA started talking and no surface was told; the orb goes on "
        "saying 'listening' over the top of her own voice")


def test_speaking_is_announced_once_per_turn_not_once_per_chunk():
    """A turn is hundreds of chunks. The state does not change hundreds of times.

    Every event is fanned out to every subscriber and crosses a WebSocket, so
    a per-chunk state event is both a lie about what changed and real traffic
    competing with the audio it is describing.
    """
    m = session()
    for _ in range(40):
        m._enqueue_audio(CHUNK)
    assert states(m).count("speaking") == 1


def test_a_new_turn_announces_speaking_again():
    m = session()
    m._enqueue_audio(CHUNK)
    m._turn_done_flag = True
    m._finish_speaking()
    m._enqueue_audio(CHUNK)
    assert states(m) == ["speaking", "listening", "speaking"]


# ── leaving it ───────────────────────────────────────────────────────────────

def test_finishing_a_turn_returns_to_listening():
    m = session()
    m._enqueue_audio(CHUNK)
    m._finish_speaking()
    assert states(m)[-1] == "listening"


def test_barge_in_returns_to_listening():
    m = session()
    m._enqueue_audio(CHUNK)
    m._barge_in()
    assert states(m)[-1] == "listening"


def test_a_dead_speaker_never_claims_to_be_speaking():
    """No speaker means no sound. Saying 'speaking' would be inventing it."""
    m = session()
    m._speaker_alive = False
    m._enqueue_audio(CHUNK)
    assert "speaking" not in states(m)


# ── and every surface has to be able to draw them ────────────────────────────

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def published_states() -> set[str]:
    """Every `state=` value LiveManager can publish, read from the source.

    Read rather than listed, so a state added later is caught by this test
    instead of quietly defaulting to idle on half the surfaces.
    """
    src = (ROOT / "desk" / "live_session.py").read_text(encoding="utf-8")
    return set(re.findall(r'state=["\']([a-z_]+)["\']', src))


def test_the_event_bus_can_name_every_state_the_session_publishes():
    """A state missing from the map became "idle" — an orb asleep mid-sentence.

    The ambient bar and the telemetry panel read the unified bus rather than
    opening their own voice socket, so this mapping is the only thing that
    tells them what NOVA is doing.
    """
    bridge = (ROOT / "desk" / "bridge.py").read_text(encoding="utf-8")
    block = bridge.split("orb_map = {", 1)[1].split("}", 1)[0]
    mapped = set(re.findall(r'"([a-z_]+)"\s*:', block))
    missing = published_states() - mapped
    assert not missing, f"states with no orb mapping, so drawn as idle: {sorted(missing)}"


def test_every_mapped_orb_state_is_one_the_orb_can_draw():
    bridge = (ROOT / "desk" / "bridge.py").read_text(encoding="utf-8")
    block = bridge.split("orb_map = {", 1)[1].split("}", 1)[0]
    targets = set(re.findall(r':\s*"([a-z_]+)"', block))

    orb = (ROOT / "desk" / "static" / "orb.js").read_text(encoding="utf-8")
    table = orb.split("const STATES = {", 1)[1].split("};", 1)[0]
    known = set(re.findall(r"^\s*([a-z_]+)\s*:", table, re.M))
    known |= set(re.findall(r"STATES\.([a-z_]+)\s*=", orb))

    unknown = targets - known
    assert not unknown, f"orb has no such state, so setState ignores it: {sorted(unknown)}"


def test_speaking_is_drawn_as_speaking_not_as_idle():
    bridge = (ROOT / "desk" / "bridge.py").read_text(encoding="utf-8")
    block = bridge.split("orb_map = {", 1)[1].split("}", 1)[0]
    assert re.search(r'"speaking"\s*:\s*"speaking"', block), (
        "NOVA talking looks the same as NOVA doing nothing")


# ── shutting down is not a failure ───────────────────────────────────────────

def manager_at(state):
    m = ls.LiveManager.__new__(ls.LiveManager)
    m._state = state
    m._state_lock = threading.Lock()
    m._subscribers = []
    m._subs_lock = threading.Lock()
    m._last_error = ""
    m._published = []
    m._publish = lambda ev: m._published.append(ev)
    return m


def _die(m):
    """Run the session loop against a coroutine that ends the way stop() ends it."""
    async def boom():
        raise RuntimeError("Event loop stopped before Future completed.")
    m._connect_and_run = boom
    m._run_loop()


def test_a_clean_shutdown_does_not_report_an_error():
    """stop() ends the session by stopping the event loop, which makes
    run_until_complete raise. That was published as a fatal error on the way
    out of every clean shutdown: red orb, "voice session failed", and the
    state set to ERROR underneath stop() as it set CLOSED."""
    m = manager_at(ls.LiveState.DISCONNECTING)
    _die(m)
    assert states(m) == [], "switching NOVA off looked like NOVA breaking"
    assert m._state is ls.LiveState.DISCONNECTING, "shutdown was overwritten"


def test_a_loop_that_dies_while_running_still_reports_it():
    """The other direction matters just as much: a session that falls over on
    its own must not be mistaken for someone closing the window."""
    m = manager_at(ls.LiveState.STREAMING)
    _die(m)
    assert states(m) == ["error"]
    assert m._state is ls.LiveState.ERROR
