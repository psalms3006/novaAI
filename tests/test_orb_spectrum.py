"""The orb's shader has had full 8-band spectrum reactivity since it was
written (orb3d.js: uBands, bandAt(), setSpectrum()), and nothing has ever
called setSpectrum() -- only a single scalar peak level has ever been
published. That shader code has been dead since it landed.

desk.live_session._spectrum_bands() computes a real FFT-derived 8-band
magnitude spectrum from the same PCM chunk already measured for the peak
level, so the orb has something real to deform against instead of a
uniform pulse.
"""
from __future__ import annotations

import numpy as np

from desk.live_session import _spectrum_bands


def test_silence_produces_all_zero_bands():
    silence = np.zeros(2000, dtype=np.int16)
    assert _spectrum_bands(silence, 0.0) == [0.0] * 8


def test_a_quiet_chunk_below_the_floor_is_treated_as_silence():
    quiet = (np.sin(2 * np.pi * 440 * np.arange(2000) / 24000) * 50).astype(np.int16)
    assert _spectrum_bands(quiet, level=0.005) == [0.0] * 8


def test_a_low_frequency_tone_peaks_in_a_low_band():
    fs = 24000
    t = np.arange(2000) / fs
    tone = (np.sin(2 * np.pi * 200 * t) * 20000).astype(np.int16)
    bands = _spectrum_bands(tone, level=0.6)
    assert bands.index(max(bands)) <= 3


def test_a_high_frequency_tone_peaks_in_a_high_band():
    fs = 24000
    t = np.arange(2000) / fs
    tone = (np.sin(2 * np.pi * 6000 * t) * 20000).astype(np.int16)
    bands = _spectrum_bands(tone, level=0.6)
    assert bands.index(max(bands)) >= 5


def test_bands_are_always_eight_values_in_range():
    fs = 24000
    t = np.arange(1600) / fs
    tone = (np.sin(2 * np.pi * 1000 * t) * 15000).astype(np.int16)
    bands = _spectrum_bands(tone, level=0.5)
    assert len(bands) == 8
    assert all(0.0 <= b <= 1.0 for b in bands)


def test_an_empty_chunk_does_not_raise():
    empty = np.zeros(0, dtype=np.int16)
    assert _spectrum_bands(empty, 0.0) == [0.0] * 8


def test_a_chunk_too_short_to_bucket_does_not_raise():
    tiny = np.array([1000, -1000, 500], dtype=np.int16)
    bands = _spectrum_bands(tiny, level=0.5)
    assert bands == [0.0] * 8
