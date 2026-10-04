"""NOVA must survive her speaker being unplugged.

Reported exactly this way: headphones out mid-conversation, and from that
moment she said nothing at all — not by voice, and not for typed messages
either. The log shows what happened:

    [LIVE] speaker write failed: Unanticipated host error [PaErrorCode -9999]:
           'There is no driver installed on your system.' [MME error 6]

repeated for as long as audio kept arriving. The stream was destroyed with the
device, the playback loop logged each failure and wrote to it again, and the
device was never reopened.

The second half matters as much: `speaking` mutes the microphone, and it stays
set while audio is still being handed over. So a speaker that died also stopped
NOVA hearing anything.
"""
from __future__ import annotations

import queue as q
import threading

import nova_voice
from desk import live_session as ls


class DeadStream:
    """An output stream that dies after `alive_writes` writes."""

    def __init__(self, alive_writes=0):
        self.remaining = alive_writes
        self.aborted = False
        self.closed = False
        self.written = 0

    def write(self, chunk):
        if self.remaining <= 0:
            raise Exception("Unanticipated host error [PaErrorCode -9999]: "
                            "'There is no driver installed on your system.'")
        self.remaining -= 1
        self.written += 1

    def abort(self): self.aborted = True
    def close(self): self.closed = True
    def start(self): pass
    def stop(self): pass


def manager(reopens_to=None):
    m = ls.LiveManager.__new__(ls.LiveManager)
    m._subscribers = []
    m._subs_lock = threading.Lock()
    m._published = []
    m._publish = lambda ev: m._published.append(ev)
    m._play_q = q.Queue()
    m._stream_lock = threading.Lock()
    m._playing_until = 99.0
    m._speaker_alive = True
    m._speaker_reopens = 0
    m._gate = nova_voice.VoiceGate(chunk_samples=1024, on_barge_in=lambda: None)
    m._opened = []
    fresh = reopens_to if reopens_to is not None else DeadStream(alive_writes=99)
    def _open():
        m._opened.append(fresh)
        return fresh
    m._open_output_stream = _open
    return m


def events(m, kind):
    return [e for e in m._published if e.type == kind]


# ── recovery ─────────────────────────────────────────────────────────────────

def test_a_dead_device_is_reopened_not_written_to_again():
    m = manager()
    dead = DeadStream(alive_writes=0)
    assert m._reopen_output(dead, Exception("no driver")) is True
    assert dead.aborted, "the dead stream was left open"
    assert len(m._opened) == 1, "no new device was opened"
    assert m._speaker_alive


def test_reopening_reports_the_new_device():
    """A speaker changing under the user is worth showing, not hiding."""
    m = manager()
    m._reopen_output(DeadStream(), Exception("no driver"))
    assert events(m, "speaker_changed")


def test_a_broken_speaker_never_leaves_NOVA_deaf():
    """`speaking` mutes the microphone. A speaker that died must not take
    her hearing with it — that is the "I removed the headphones and then
    nothing at all" half of the report."""
    m = manager()
    m._gate.set_speaking(True)
    m._reopen_output(DeadStream(), Exception("no driver"))
    assert m._gate.speaking is False


def test_stale_audio_for_a_device_that_no_longer_exists_is_dropped():
    m = manager()
    for _ in range(5):
        m._play_q.put(b"\x00\x00" * 512)
    m._reopen_output(DeadStream(), Exception("no driver"))
    assert m._play_q.empty()


# ── giving up ────────────────────────────────────────────────────────────────

def test_it_gives_up_eventually_rather_than_spinning():
    """A device that has genuinely gone must not be retried forever."""
    m = manager()
    outcomes = [m._reopen_output(DeadStream(), Exception("gone"))
                for _ in range(ls.LiveManager.MAX_SPEAKER_REOPENS + 1)]
    assert outcomes[-1] is False
    assert not m._speaker_alive


def test_giving_up_says_so_instead_of_going_quiet():
    m = manager()
    for _ in range(ls.LiveManager.MAX_SPEAKER_REOPENS + 1):
        m._reopen_output(DeadStream(), Exception("gone"))
    assert events(m, "error"), "NOVA lost her speaker and told nobody"
    said = " ".join(str(e.data) for e in m._published).lower()
    assert "headphones" in said or "speakers" in said


def test_it_allows_more_than_one_attempt():
    """Windows does not settle on a new default the instant the old one dies."""
    assert ls.LiveManager.MAX_SPEAKER_REOPENS > 1


# ── and the API it opens on ──────────────────────────────────────────────────

def test_audio_prefers_wasapi_over_mme_where_available():
    """MME is a compatibility shim: 90 ms minimum latency against WASAPI's 3 ms
    on this machine's own speaker, coarse buffering heard as crackling, and no
    useful report when a device disappears."""
    api = ls._preferred_host_api()
    if api is None:
        return                      # not Windows, or no WASAPI here
    import sounddevice as sd
    assert "wasapi" in sd.query_hostapis(api)["name"].lower()

    out = ls._speaker_device()
    if out is None:
        return                      # nothing matched; the default is still fine
    assert sd.query_devices(out)["hostapi"] == api, (
        "the speaker is still being opened through MME")


def test_the_speaker_device_is_resolved_fresh_every_time():
    """This is the reconnect path too. When a device disappears the answer to
    'which speaker now' has changed, and a cached index is the one that died."""
    import inspect
    src = inspect.getsource(ls.LiveManager._open_output_stream)
    assert "_speaker_device()" in src


# ── the fallback chain itself ────────────────────────────────────────────────

def test_the_system_default_is_a_device_not_a_failure():
    """None means "the system default" and is the fallback that matters most.

    An earlier version of this chain used None as its "nothing opened"
    sentinel, so a *successful* fallback open was read as a failure and the
    first attempt's error was raised over the top of a working microphone.
    NOVA reported mic: False with the microphone wide open.
    """
    import inspect
    src = inspect.getsource(ls.LiveManager._start_mic)
    body = src.split('"""')[-1] if '"""' in src else src
    assert "if not got:" in body, (
        "success is being inferred from the device value again")


def test_wasapi_input_failure_falls_back_rather_than_giving_up():
    """This machine's driver refuses WASAPI input outright, answering with a
    WDM-KS ioctl error. The next attempt is the one that has always worked,
    and NOVA must reach it."""
    import inspect
    src = inspect.getsource(ls.LiveManager._start_mic)
    assert "_mic_device()" in src, "there is no fallback attempt at all"
    assert src.index("_mic_device_resolved()") < src.index("attempts.append((_mic_device()"), (
        "the fallback is tried before the preferred device")


# ── telling "we starved it" from "the device mangled it" ─────────────────────

def test_starvation_is_counted_and_reported():
    """"It crackles" has two halves and they need different fixes.

    If the queue empties while the model is still sending, NOVA is starving
    the device and the gap is hers. If this stays at zero and the user still
    hears breakup, the audio left here intact and the fault is downstream —
    the device, its driver, or a Bluetooth link. Without the number there is
    no way to tell those apart except by guessing.
    """
    import inspect
    src = inspect.getsource(ls.LiveManager._start_playback)
    assert "self._starved" in src

    status_src = inspect.getsource(ls.LiveManager.status)
    assert "speaker_starved" in status_src


def test_a_finished_turn_is_not_counted_as_starvation():
    """The queue emptying at the end of a reply is the reply ending."""
    import inspect
    src = inspect.getsource(ls.LiveManager._start_playback)
    guard = src.split("self._starved += 1")[0]
    assert "not self._turn_done_flag" in guard.split("except queue.Empty:")[-1]
