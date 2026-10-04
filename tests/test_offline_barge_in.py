"""Offline TTS playback must be interruptible by real voice, not just a
button -- and must not stop for its own echo.

offline_extra._play_pcm_with_barge_in() is the shared playback path both
_speak_pyttsx3 and _speak_piper go through. It reuses
nova_voice.VoiceGate/EchoCanceller -- the same mechanism the (default-off)
full-duplex cloud path uses -- rather than a fresh echo/VAD heuristic, so
these tests pin the *wiring* (reference fed per chunk, mic callback fires
concurrently with playback, playback stops the moment the gate calls
on_barge_in) rather than re-proving VoiceGate's own echo rejection, which
tests/test_voice_gate_echo.py already covers against real recorded audio.

The mic is opened as a callback-based sd.InputStream (see the module-level
note in offline_extra.py on why a blocking-read thread was replaced with
this), so the fake mic here runs the production callback on its own
background thread, the way PortAudio actually would, rather than being
polled from the test.

No real audio hardware is available in this environment, so sounddevice's
InputStream/OutputStream and desk.live_session's device resolution are
replaced with fakes that behave like the real things but run entirely
in-process.
"""
from __future__ import annotations

import threading
import time

import numpy as np
import pytest

import offline_extra


class FakeGate:
    """Stands in for nova_voice.VoiceGate: fires on_barge_in on command."""

    def __init__(self, on_barge_in, fire_after: int | None = None):
        self._on_barge_in = on_barge_in
        self._fire_after = fire_after
        self.process_calls = 0
        self.reference_calls = 0
        self.speaking_history: list[tuple[bool, bool]] = []
        self._lock = threading.Lock()

    def set_speaking(self, value: bool, interrupted: bool = False) -> None:
        self.speaking_history.append((value, interrupted))

    def reference(self, pcm: bytes, rate: int) -> None:
        self.reference_calls += 1

    def process(self, frame: np.ndarray) -> bytes:
        with self._lock:
            self.process_calls += 1
            fire = (self._fire_after is not None
                    and self.process_calls >= self._fire_after)
        if fire:
            self._on_barge_in()
        return frame.tobytes()


class FakeInputStream:
    """Stands in for a callback-based sd.InputStream: runs the production
    callback on its own background thread once started, the way PortAudio
    actually schedules it -- concurrently with whatever the caller does
    next, not polled from it."""

    def __init__(self, callback=None, **kw):
        self._callback = callback
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.started = False
        self.stopped = False
        self.closed = False

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._callback is not None:
                self._callback(np.zeros((1024, 1), dtype=np.int16), 1024,
                               None, None)
            time.sleep(0.001)

    def start(self) -> None:
        self.started = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stopped = True
        self._stop.set()

    def close(self) -> None:
        self.closed = True
        if self._thread is not None:
            self._thread.join(timeout=1.0)


class FakeOutputStream:
    """Records every chunk written. `delay_s` gives the mic's background
    thread time to react before the caller checks whether it should stop."""

    def __init__(self, delay_s: float = 0.0, **kw):
        self.chunks_written = 0
        self._delay_s = delay_s

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write(self, block):
        if self._delay_s:
            time.sleep(self._delay_s)
        self.chunks_written += 1


def _install_fakes(monkeypatch, gate, out_stream):
    monkeypatch.setattr(offline_extra, "_get_offline_gate", lambda: gate)
    monkeypatch.setattr(offline_extra, "_offline_input_device",
                        lambda: (None, None))
    monkeypatch.setattr(offline_extra, "_offline_output_device",
                        lambda: (None, None))
    monkeypatch.setattr(offline_extra.sd, "InputStream",
                        lambda **kw: FakeInputStream(**kw))
    monkeypatch.setattr(offline_extra.sd, "OutputStream",
                        lambda **kw: out_stream)


def test_playback_completes_normally_when_nothing_interrupts(monkeypatch):
    gate = FakeGate(offline_extra._on_offline_barge_in, fire_after=None)
    out = FakeOutputStream(delay_s=0.0)
    _install_fakes(monkeypatch, gate, out)

    audio = np.zeros(1024 * 10, dtype=np.int16)
    interrupted = offline_extra._play_pcm_with_barge_in(audio, 22050)

    assert interrupted is False
    assert out.chunks_written == 10
    assert gate.reference_calls == 10
    assert gate.speaking_history[0] == (True, False)
    assert gate.speaking_history[-1] == (False, False)


def test_playback_stops_early_on_a_genuine_barge_in(monkeypatch):
    gate = FakeGate(offline_extra._on_offline_barge_in, fire_after=1)
    out = FakeOutputStream(delay_s=0.01)
    _install_fakes(monkeypatch, gate, out)

    audio = np.zeros(1024 * 50, dtype=np.int16)
    interrupted = offline_extra._play_pcm_with_barge_in(audio, 22050)

    assert interrupted is True
    assert out.chunks_written < 50, (
        "playback ran to completion instead of stopping when the gate "
        "reported a barge-in"
    )
    assert gate.speaking_history[-1] == (False, True)


def test_mic_callback_fires_concurrently_with_playback(monkeypatch):
    """A barge-in has to be heard while NOVA is talking, not polled for
    between chunks -- so the mic's callback must actually be running while
    the output loop is still writing."""
    gate = FakeGate(offline_extra._on_offline_barge_in, fire_after=None)
    out = FakeOutputStream(delay_s=0.02)
    _install_fakes(monkeypatch, gate, out)

    audio = np.zeros(1024 * 5, dtype=np.int16)
    offline_extra._play_pcm_with_barge_in(audio, 22050)

    assert gate.process_calls > 0, (
        "the mic callback never fired during playback"
    )


def test_mic_stream_is_closed_after_playback(monkeypatch):
    gate = FakeGate(offline_extra._on_offline_barge_in, fire_after=None)
    out = FakeOutputStream(delay_s=0.0)
    created: list[FakeInputStream] = []

    def _capturing_input_stream(**kw):
        s = FakeInputStream(**kw)
        created.append(s)
        return s

    monkeypatch.setattr(offline_extra, "_get_offline_gate", lambda: gate)
    monkeypatch.setattr(offline_extra, "_offline_input_device",
                        lambda: (None, None))
    monkeypatch.setattr(offline_extra, "_offline_output_device",
                        lambda: (None, None))
    monkeypatch.setattr(offline_extra.sd, "InputStream", _capturing_input_stream)
    monkeypatch.setattr(offline_extra.sd, "OutputStream", lambda **kw: out)

    audio = np.zeros(1024 * 3, dtype=np.int16)
    offline_extra._play_pcm_with_barge_in(audio, 22050)

    assert len(created) == 1
    assert created[0].stopped is True
    assert created[0].closed is True


def test_output_stream_failure_propagates_instead_of_reporting_success(monkeypatch):
    """A TTS engine's caller (_speak_pyttsx3/_speak_piper) must see a failed
    output device as a failure, so it falls through to the next engine
    instead of reporting success on audio that never played."""
    gate = FakeGate(offline_extra._on_offline_barge_in, fire_after=None)

    def _broken_output_stream(**kw):
        raise RuntimeError("Invalid sample rate")

    monkeypatch.setattr(offline_extra, "_get_offline_gate", lambda: gate)
    monkeypatch.setattr(offline_extra, "_offline_input_device",
                        lambda: (None, None))
    monkeypatch.setattr(offline_extra, "_offline_output_device",
                        lambda: (None, None))
    monkeypatch.setattr(offline_extra.sd, "InputStream",
                        lambda **kw: FakeInputStream(**kw))
    monkeypatch.setattr(offline_extra.sd, "OutputStream", _broken_output_stream)

    audio = np.zeros(1024 * 3, dtype=np.int16)
    with pytest.raises(RuntimeError):
        offline_extra._play_pcm_with_barge_in(audio, 22050)
