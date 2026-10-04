"""listen_offline() must distinguish "nothing to react to" from "a real
utterance NOVA genuinely could not transcribe", and must not treat noise
scattered across a long recording as speech.

Root-caused from a real run (2026-09-22): the terminal repeatedly showed
"Transcribing..." while the user was silent, and NOVA then looped
"I didn't catch that" -- answered by TTS, which the offline mic (see
tests/test_offline_barge_in.py's module docstring for the device-selection
and mic-timing issues found alongside this) could plausibly hear as more
"speech". Two changes here address the STT/VAD side of that:

1. listen_offline() now returns None (nothing to say) for true silence, a
   mic failure, or the STT subsystem not being ready, and reserves "" for
   the one case that should actually prompt "I didn't catch that": a real
   utterance was captured and STT ran but produced no usable text. Every
   existing caller already does `if not user_input:`-style checks, which
   treat None and "" identically, so this is additive, not breaking, except
   at the one call site (run_offline_loop_v2) meant to use the difference.
2. A capture only counts as speech if the loud chunks are reasonably
   continuous within the span they occupy (MIN_SPEECH_DENSITY), not merely
   present somewhere in a long, mostly-silent recording.
"""
from __future__ import annotations

import threading

import numpy as np
import pytest

import offline_extra


class _PatternStream:
    """Replays an explicit loud/silent chunk pattern, then holds silent."""

    def __init__(self, pattern: list[bool], chunk_size: int):
        self._pattern = pattern
        self._i = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n):
        loud = self._pattern[self._i] if self._i < len(self._pattern) else False
        self._i += 1
        amp = 1.0 if loud else 0.0
        return (np.full((n, 1), amp, dtype=np.float32), False)


class _EmptySegments:
    def transcribe(self, path, **kwargs):
        return ([], None)


def _prime(monkeypatch, pattern: list[bool]):
    monkeypatch.setattr(offline_extra.sd, "InputStream",
                        lambda **kw: _PatternStream(pattern, kw["blocksize"]))
    monkeypatch.setattr(offline_extra._nova, "AMBIENT_THRESHOLD", 0.04)
    monkeypatch.setattr(offline_extra, "LISTEN_SETTLE_S", 0.0)
    monkeypatch.setattr(offline_extra, "_offline_input_device",
                        lambda: (None, None))


def test_scattered_noise_is_not_treated_as_speech(monkeypatch):
    # 10 loud chunks, but each isolated between 2 silent ones -- present
    # "somewhere" in the recording, never continuous -- then a real
    # silence tail to end the capture.
    burst = ([True] + [False, False]) * 10
    pattern = burst + [False] * 30
    _prime(monkeypatch, pattern)

    loaded = threading.Event()
    loaded.set()
    monkeypatch.setattr(offline_extra, "_stt_loaded", loaded)

    result = offline_extra.listen_offline()

    assert result is None, (
        "scattered noise satisfied the raw speech-chunk count and was "
        "sent to STT instead of being discarded as noise"
    )


def test_continuous_speech_with_no_usable_transcript_returns_empty_string(monkeypatch):
    pattern = [True] * 15 + [False] * 30
    _prime(monkeypatch, pattern)

    loaded = threading.Event()
    loaded.set()
    monkeypatch.setattr(offline_extra, "_stt_loaded", loaded)
    monkeypatch.setattr(offline_extra, "_stt_model_lock", threading.Lock())
    monkeypatch.setattr(offline_extra._nova, "_stt_model", _EmptySegments())

    result = offline_extra.listen_offline()

    assert result == "", (
        "a real utterance that STT could not transcribe must return \"\" "
        "(the one case that should prompt \"I didn't catch that\"), not None"
    )


def test_brief_speech_that_never_reaches_minimum_duration_returns_none(monkeypatch):
    # 3 loud chunks -- well under min_speech_r (9) -- then real silence.
    pattern = [True] * 3 + [False] * 30
    _prime(monkeypatch, pattern)

    result = offline_extra.listen_offline()

    assert result is None, (
        "silence (never reaching the minimum speech duration) must return "
        "None, not \"\" -- returning \"\" here is what made NOVA say "
        "\"I didn't catch that\" to an empty room"
    )


def test_stt_not_ready_returns_none_not_empty_string(monkeypatch):
    """The STT subsystem not being ready is a subsystem failure, not the
    user's silence -- it must not be reported as "I didn't catch that"."""
    pattern = [True] * 15 + [False] * 30
    _prime(monkeypatch, pattern)

    not_loaded = threading.Event()  # never set -- times out
    monkeypatch.setattr(offline_extra, "_stt_loaded", not_loaded)

    import time as _t
    start = _t.monotonic()
    result = offline_extra.listen_offline()
    elapsed = _t.monotonic() - start

    assert result is None
    assert elapsed < 15, "the 10s wait for _stt_loaded should not be exceeded"
