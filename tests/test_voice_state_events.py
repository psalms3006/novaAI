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
    m._suppress_until = 0.0
    m._suppressed_tail = 0
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

# The checks on the previous window (desk/static) that lived here moved to
# tests/test_desk_ui_behaviour.py when the interface became desk/ui.

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


# ── one session does not haunt the next ──────────────────────────────────────

def test_stopping_leaves_nothing_of_the_session_behind():
    """A stopped session's loop and queues must not outlive it.

    They did, and a straggler from the old session — a receive coroutine the
    SDK had not finished unwinding — could still reach for them. Once that
    loop is closed, touching it raises "Event loop is closed", which was
    observed published as a fatal voice error moments before a perfectly
    healthy reconnect.
    """
    m = ls.LiveManager.__new__(ls.LiveManager)
    m._state = ls.LiveState.STREAMING
    m._state_lock = threading.Lock()
    m._subscribers = []
    m._subs_lock = threading.Lock()
    m._published = []
    m._publish = lambda ev: m._published.append(ev)
    m._screen = type("S", (), {"stop": lambda self: None, "enabled": False})()
    class _Loop:
        def is_closed(self): return False
        def is_running(self): return False
        def call_soon_threadsafe(self, fn, *a): pass
    m._loop = _Loop()
    m._thread = None
    m._mic_queue = q.Queue()
    m._input_text_queue = q.Queue()
    m._video_queue = q.Queue()
    m._session = object()
    m._start_time = 0.0
    m._turn_count = 0
    m._mic_active = False
    m._mic_stream = None
    m._send_count = 0
    m._mic_dropped = 0
    m._play_stop = threading.Event()
    m._play_q = q.Queue()
    m._play_thread = None
    m._gate = nova_voice.VoiceGate(chunk_samples=1024, on_barge_in=lambda: None)

    m.stop()

    for name in ("_loop", "_mic_queue", "_input_text_queue", "_video_queue",
                 "_session", "_thread"):
        assert getattr(m, name) is None, f"{name} survived the session it belonged to"
    assert states(m)[-1] == "closed"


# ── the speaker has to survive a restart ─────────────────────────────────────

def playback_manager():
    m = ls.LiveManager.__new__(ls.LiveManager)
    m._state = ls.LiveState.STREAMING
    m._state_lock = threading.Lock()
    m._subscribers = []
    m._subs_lock = threading.Lock()
    m._published = []
    m._publish = lambda ev: m._published.append(ev)
    m._screen = type("S", (), {"stop": lambda self: None, "enabled": False})()
    class _Loop:
        def is_closed(self): return False
        def is_running(self): return False
        def call_soon_threadsafe(self, fn, *a): pass
    m._loop = _Loop()
    m._thread = None
    m._mic_queue = q.Queue()
    m._input_text_queue = q.Queue()
    m._video_queue = q.Queue()
    m._session = None
    m._start_time = 0.0
    m._turn_count = 0
    m._mic_active = False
    m._mic_stream = None
    m._send_count = 0
    m._mic_dropped = 0
    m._play_stop = threading.Event()
    m._play_q = q.Queue()
    m._play_thread = threading.Thread(target=lambda: None)
    m._play_thread.start()
    m._speaker_alive = True
    m._gate = nova_voice.VoiceGate(chunk_samples=1024, on_barge_in=lambda: None)
    return m


def test_stopping_releases_the_speaker():
    """The bug that made NOVA permanently mute after one restart.

    stop() ends the session by stopping the event loop, and a coroutine
    suspended when its loop stops is never resumed — so the `finally` in
    _connect_and_run that used to tear playback down never ran.
    `_play_thread` stayed set, and the *next* session hit the guard at the top
    of _start_playback, returned False without logging anything, and ran with
    no speaker at all. Every session after the first reported "speaker
    unavailable" and skipped the greeting, until the app itself was restarted.
    """
    m = playback_manager()
    m.stop()
    assert m._play_thread is None, (
        "the speaker thread outlived its session; the next one will find the "
        "device taken and run mute")
    assert m._speaker_alive is False


def test_a_left_over_speaker_thread_is_cleared_rather_than_refused():
    """Whatever leaves a worker behind, the answer is to open the speaker.

    A session that cannot talk is not a session, and it must never be reached
    by returning False from a bare guard with nothing in the log.
    """
    import inspect
    src = inspect.getsource(ls.LiveManager._start_playback)
    head = src.split('"""')[2]                     # past the docstring
    guard = head.split("if self._play_thread is not None:", 1)
    assert len(guard) == 2, "the stale-thread guard is gone entirely"
    body = guard[1].split("\n\n", 1)[0]
    assert "_stop_playback" in body, (
        "a left-over thread still makes the session silently mute")
