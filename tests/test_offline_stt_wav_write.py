"""``listen_offline`` must be able to write the audio it just captured.

`from scipy.io import wavfile as wav_write` binds `wav_write` to the
*module*, not its `write` function. `listen_offline` called it directly --
`wav_write(tmp_path, fs, audio_i16)` -- which raises `TypeError: 'module'
object is not callable` every single time, and only when it matters: this
line only runs once real speech has actually been captured. The raise sits
outside the function's own try/except (that block starts after the WAV is
written), so it propagates out of `listen_offline` uncaught and out of
`run_offline_loop_v2`'s `while True:` after it, killing the whole offline
loop -- and with it the `python nova.py` process -- the first time anyone
actually spoke to NOVA in offline voice mode.
"""
from __future__ import annotations

import threading

import numpy as np
import pytest

import offline_extra


class _FakeStream:
    """Stands in for `sd.InputStream`: yields loud frames, then silence."""

    def __init__(self, loud_frames: int, chunk_size: int):
        self._loud_frames = loud_frames
        self._chunk_size = chunk_size
        self._calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n):
        self._calls += 1
        loud = self._calls <= self._loud_frames
        amp = 1.0 if loud else 0.0
        return (np.full((n, 1), amp, dtype=np.float32), False)


class _FakeSegment:
    text = " hello nova "


class _FakeSTTModel:
    def transcribe(self, path, **kwargs):
        # Prove the file this module wrote is actually readable, the way a
        # real faster-whisper model would need it to be.
        with open(path, "rb") as f:
            header = f.read(4)
        assert header == b"RIFF", "the WAV write must have actually happened"
        return ([_FakeSegment()], None)


def test_listen_offline_can_write_the_captured_audio(monkeypatch):
    monkeypatch.setattr(offline_extra.sd, "InputStream",
                        lambda **kw: _FakeStream(loud_frames=15,
                                                  chunk_size=kw["blocksize"]))
    monkeypatch.setattr(offline_extra._nova, "AMBIENT_THRESHOLD", 0.04)

    loaded = threading.Event()
    loaded.set()
    monkeypatch.setattr(offline_extra, "_stt_loaded", loaded)
    monkeypatch.setattr(offline_extra, "_stt_model_lock", threading.Lock())
    monkeypatch.setattr(offline_extra._nova, "_stt_model", _FakeSTTModel())

    transcript = offline_extra.listen_offline()

    assert transcript == "hello nova"
