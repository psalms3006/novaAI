"""Offline TTS playback must be interruptible by real voice, not just a
button -- and must not stop for its own echo.

offline_extra._play_pcm_with_barge_in() is the shared playback path both
_speak_pyttsx3 and _speak_piper now go through. It reuses
nova_voice.VoiceGate/EchoCanceller -- the same mechanism the (default-off)
full-duplex cloud path uses -- rather than a fresh echo/VAD heuristic, so
these tests pin the *wiring* (reference fed per chunk, mic fed concurrently,
playback stops the moment the gate calls on_barge_in) rather than
re-proving VoiceGate's own echo rejection, which tests/test_voice_gate_echo.py
already covers against real recorded audio.

No real audio hardware is available in this environment, so sounddevice's
InputStream/OutputStream are replaced with fakes that behave like the real
context managers but run entirely in-process.
"""
from __future__ import annotations

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

    def set_speaking(self, value: bool, interrupted: bool = False) -> None:
        self.speaking_history.append((value, interrupted))

    def reference(self, pcm: bytes, rate: int) -> None:
        self.reference_calls += 1

    def process(self, frame: np.ndarray) -> bytes:
        self.process_calls += 1
        if self._fire_after is not None and self.process_calls >= self._fire_after:
            self._on_barge_in()
        return frame.tobytes()


class FakeInputStream:
    """No-delay mic: loops fast so the mic thread gets many chances to fire
    before the (deliberately slowed) output loop finishes its first chunk."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n):
        return (np.zeros((n, 1), dtype=np.int16), False)


class FakeOutputStream:
    """Records every chunk written. `delay_s` gives the mic thread time to
    react before the caller checks whether it should stop."""

    def __init__(self, delay_s: float = 0.0):
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
    monkeypatch.setattr(offline_extra.sd, "InputStream",
                        lambda **kw: FakeInputStream())
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


def test_mic_is_fed_concurrently_with_playback(monkeypatch):
    """A barge-in has to be heard while NOVA is talking, not polled for
    between chunks -- so the mic thread must actually be running (calling
    gate.process) while the output loop is still writing."""
    gate = FakeGate(offline_extra._on_offline_barge_in, fire_after=None)
    out = FakeOutputStream(delay_s=0.02)
    _install_fakes(monkeypatch, gate, out)

    audio = np.zeros(1024 * 5, dtype=np.int16)
    offline_extra._play_pcm_with_barge_in(audio, 22050)

    assert gate.process_calls > 0, (
        "the mic feed thread never called gate.process() during playback"
    )
