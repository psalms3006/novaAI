"""The orb has to be able to show that the user is talking.

`audio_level` events already reach the orb, but they are published from
`LiveManager._enqueue_audio` -- NOVA's *outgoing* audio. So the orb pulses
when NOVA speaks and sits still while the user does, which is the one moment
a voice assistant most needs to look like it is listening.

The signal itself already exists and costs nothing extra. `VoiceGate._note_room`
computes `frame_rms` on every mic frame and compares it against a floor
learned from the room, because the gate needs that to decide what is worth
transmitting. It simply was never exposed.

Two constraints shape the design:

* It is read from a real-time audio callback, so nothing here may block or
  allocate much. `_publish` is `put_nowait` onto bounded queues, which is
  safe; frame-rate publishing is not, because it would flood the socket at
  ~50 Hz for no visual benefit.
* "Quiet" must mean what the gate means by it -- judged against the learned
  room floor, not a fixed number -- or the orb will react to a laptop fan.
"""
from __future__ import annotations

import numpy as np
import pytest

from nova_voice import VoiceGate, frame_rms


FRAME = 320   # 20 ms at 16 kHz


def _silence(scale=8):
    """Room tone: quiet, but never digitally zero."""
    rng = np.random.default_rng(0)
    return (rng.normal(0, scale, FRAME)).astype(np.int16)


def _speech(scale=4000):
    rng = np.random.default_rng(1)
    return (rng.normal(0, scale, FRAME)).astype(np.int16)


def _gate():
    return VoiceGate(chunk_samples=FRAME, simple=True)


def test_the_gate_reports_the_level_it_already_measured():
    """Fails until the measured amplitude is readable from outside."""
    gate = _gate()
    gate.process(_speech())
    assert hasattr(gate, "last_level"), (
        "the gate measures frame_rms on every frame and keeps it to itself"
    )
    assert 0.0 < gate.last_level <= 1.0, gate.last_level


def test_the_level_is_normalised_not_raw_int16():
    """The orb wants 0..1; int16 RMS is 0..32768."""
    gate = _gate()
    loud = (np.full(FRAME, 20000)).astype(np.int16)
    gate.process(loud)
    assert gate.last_level <= 1.0
    assert gate.last_level > 0.5, gate.last_level


def test_speech_reads_louder_than_room_tone():
    gate = _gate()
    gate.process(_silence())
    quiet_level = gate.last_level
    gate.process(_speech())
    assert gate.last_level > quiet_level * 4, (
        f"speech {gate.last_level} vs room {quiet_level}"
    )


def test_an_empty_room_is_reported_as_quiet():
    """Judged against the learned floor, so a fan is not 'hearing'."""
    gate = _gate()
    for _ in range(60):
        gate.process(_silence())
    assert gate.last_was_quiet is True
    assert gate.last_level < 0.01, gate.last_level


def test_speech_is_not_reported_as_quiet():
    gate = _gate()
    for _ in range(40):
        gate.process(_silence())       # learn the room
    gate.process(_speech())
    assert gate.last_was_quiet is False


def test_measuring_the_level_does_not_change_what_is_transmitted():
    """A read-only observation. Regressing this would break the voice path."""
    gate = _gate()
    frame = _speech()
    out = gate.process(frame)
    assert out == frame.tobytes()


# ── throttling: the part that protects the socket ───────────────────────────

def test_levels_are_rate_limited_before_they_reach_the_socket():
    from desk.live_session import MicLevelThrottle

    throttle = MicLevelThrottle(hz=12)
    now = [1000.0]
    throttle._now = lambda: now[0]

    assert throttle.should_send(0.5) is True      # first one goes
    assert throttle.should_send(0.5) is False     # same instant, suppressed

    now[0] += 0.05                                 # 50 ms, inside the 83 ms tick
    assert throttle.should_send(0.5) is False

    now[0] += 0.5
    assert throttle.should_send(0.5) is True


def test_a_sudden_onset_is_not_delayed_by_the_rate_limit():
    """The moment speech starts is the one frame worth sending immediately."""
    from desk.live_session import MicLevelThrottle

    throttle = MicLevelThrottle(hz=12)
    now = [1000.0]
    throttle._now = lambda: now[0]

    throttle.should_send(0.01)                     # quiet baseline
    now[0] += 0.01                                 # far inside the interval
    assert throttle.should_send(0.6) is True, (
        "the start of speech waited for the next tick, so the orb reacts late"
    )


def test_silence_stops_being_resent_once_it_has_settled():
    """Do not spend the socket repeating that nothing is happening."""
    from desk.live_session import MicLevelThrottle

    throttle = MicLevelThrottle(hz=12)
    now = [1000.0]
    throttle._now = lambda: now[0]

    throttle.should_send(0.0)
    sent = 0
    for _ in range(50):
        now[0] += 0.2
        if throttle.should_send(0.0):
            sent += 1
    assert sent <= 2, f"resent silence {sent} times"


# ── wiring: the signal has to actually leave the audio callback ─────────────

def test_the_microphone_callback_publishes_the_level():
    """Fails if the level is measured but never sent anywhere.

    The gate exposing `last_level` is useless on its own; the orb only
    changes if the mic callback publishes it. A source check, because
    driving a real sounddevice callback in a unit test would need a
    microphone.
    """
    import inspect

    from desk.live_session import LiveManager

    source = inspect.getsource(LiveManager._start_mic)
    assert "mic_level" in source, (
        "the microphone callback measures the level but never publishes it"
    )
    assert "_mic_level_throttle" in source, (
        "the level is published unthrottled from a real-time audio callback"
    )


def test_the_surface_distinguishes_hearing_from_listening():
    """The orb needs a distinct state, or the signal changes nothing visible."""
    from pathlib import Path

    static = Path(__file__).resolve().parents[1] / "desk" / "static"

    app = (static / "app.js").read_text(encoding="utf-8", errors="replace")
    assert '"mic_level"' in app or "case \"mic_level\"" in app, (
        "app.js ignores mic_level events"
    )

    for name in ("orb3d.js", "orb.js"):
        text = (static / name).read_text(encoding="utf-8", errors="replace")
        assert "hearing" in text, f"{name} has no hearing state to show"
