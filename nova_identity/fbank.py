"""Kaldi-compatible log-mel filterbank features, in numpy.

The speaker model wants exactly what WeSpeaker fed it during training: 80-bin
Kaldi fbank at 16 kHz, 25 ms windows every 10 ms, mean-normalised over time.
Features that are merely *similar* to that do not produce slightly worse
embeddings, they produce embeddings from a distribution the model has never
seen, and every speaker then looks equally like every other.

The reference implementation is `torchaudio.compliance.kaldi.fbank`, which
means torch. NOVA runs the model on onnxruntime precisely so the packaged
application does not have to carry torch to tell two people apart, and
importing torch for the feature step would give all of that back. So the
front end is reimplemented here against Kaldi's definition, and the constants
below are Kaldi's defaults rather than choices — changing one silently
invalidates every enrolled profile.
"""
from __future__ import annotations

import numpy as np

SAMPLE_RATE = 16000
FRAME_LENGTH_MS = 25.0
FRAME_SHIFT_MS = 10.0
NUM_MEL_BINS = 80
LOW_FREQ = 20.0
#: Kaldi's high_freq=0 means "Nyquist"; written out so it is readable.
HIGH_FREQ = SAMPLE_RATE / 2.0
PREEMPH = 0.97
#: Kaldi floors the log at this rather than at zero, so a silent frame gives a
#: large negative number instead of -inf.
LOG_FLOOR = np.finfo(np.float32).eps

_EPS = 1e-10


def _povey_window(n: int) -> np.ndarray:
    """Kaldi's default window: Hann raised to 0.85.

    Not Hamming and not Hann. Kaldi calls it "povey" and it is what the model
    was trained through.
    """
    k = np.arange(n, dtype=np.float64)
    hann = 0.5 - 0.5 * np.cos(2.0 * np.pi * k / (n - 1))
    return np.power(hann, 0.85)


def _mel(f: np.ndarray | float) -> np.ndarray | float:
    return 1127.0 * np.log(1.0 + np.asarray(f, dtype=np.float64) / 700.0)


def _mel_banks(num_bins: int, n_fft: int, rate: int) -> np.ndarray:
    """Triangular mel filters, laid out the way Kaldi lays them out.

    Kaldi places bin *centres* on a uniform mel grid and builds each triangle
    from its neighbours' centres, which is not the same as the more common
    "edges on a uniform grid" construction. The difference is small per bin
    and systematic across all of them.
    """
    num_fft_bins = n_fft // 2
    mel_low, mel_high = _mel(LOW_FREQ), _mel(HIGH_FREQ)
    delta = (mel_high - mel_low) / (num_bins + 1)

    # Frequency of each FFT bin, in mel.
    fft_freqs = np.arange(num_fft_bins, dtype=np.float64) * (rate / n_fft)
    fft_mel = _mel(fft_freqs)

    banks = np.zeros((num_bins, num_fft_bins), dtype=np.float64)
    for b in range(num_bins):
        left = mel_low + b * delta
        centre = left + delta
        right = centre + delta
        up = (fft_mel - left) / (centre - left)
        down = (right - fft_mel) / (right - centre)
        banks[b] = np.maximum(0.0, np.minimum(up, down))
    return banks


def _frames(signal: np.ndarray, length: int, shift: int) -> np.ndarray:
    """Split into overlapping frames, discarding a short tail (snip_edges)."""
    if len(signal) < length:
        return np.empty((0, length), dtype=np.float64)
    n = 1 + (len(signal) - length) // shift
    idx = np.arange(length)[None, :] + shift * np.arange(n)[:, None]
    return signal[idx]


class FbankExtractor:
    """Turns 16 kHz mono int16 or float audio into (frames, 80) features.

    Stateless apart from the filterbank and window, which are computed once
    because they are the expensive part and never change.
    """

    def __init__(self, rate: int = SAMPLE_RATE, num_mel_bins: int = NUM_MEL_BINS):
        self.rate = rate
        self.num_mel_bins = num_mel_bins
        self.frame_length = int(rate * FRAME_LENGTH_MS / 1000.0)   # 400
        self.frame_shift = int(rate * FRAME_SHIFT_MS / 1000.0)     # 160
        # Kaldi rounds the FFT size up to a power of two, so 400 -> 512.
        self.n_fft = 1
        while self.n_fft < self.frame_length:
            self.n_fft *= 2
        self._window = _povey_window(self.frame_length)
        self._banks = _mel_banks(num_mel_bins, self.n_fft, rate)

    def __call__(self, audio: np.ndarray, cmn: bool = True) -> np.ndarray:
        return self.extract(audio, cmn=cmn)

    def extract(self, audio: np.ndarray, cmn: bool = True) -> np.ndarray:
        """Features for one utterance, shaped (frames, num_mel_bins).

        `cmn` subtracts the per-bin mean over time, which is what WeSpeaker
        does before the network sees anything. It is what makes the embedding
        describe the speaker rather than the microphone and the room.
        """
        x = np.asarray(audio)
        if x.ndim > 1:
            x = x.reshape(-1)
        if x.dtype == np.int16:
            # Kaldi works on the raw integer scale, not on [-1, 1).
            x = x.astype(np.float64)
        else:
            x = x.astype(np.float64)
            if np.max(np.abs(x)) <= 1.0:
                x = x * 32768.0

        frames = _frames(x, self.frame_length, self.frame_shift)
        if frames.shape[0] == 0:
            return np.zeros((0, self.num_mel_bins), dtype=np.float32)

        # Per frame, in Kaldi's order: remove DC, pre-emphasise, window.
        frames = frames - frames.mean(axis=1, keepdims=True)
        # Pre-emphasis uses each frame's own first sample as the predecessor,
        # which is what Kaldi does after offset removal.
        prev = np.concatenate([frames[:, :1], frames[:, :-1]], axis=1)
        frames = frames - PREEMPH * prev
        frames = frames * self._window

        spectrum = np.fft.rfft(frames, n=self.n_fft)
        power = np.abs(spectrum[:, : self.n_fft // 2]) ** 2

        energies = power @ self._banks.T
        feats = np.log(np.maximum(energies, LOG_FLOOR))

        if cmn and feats.shape[0] > 0:
            feats = feats - feats.mean(axis=0, keepdims=True)
        return feats.astype(np.float32)


_default: FbankExtractor | None = None


def fbank(audio: np.ndarray, cmn: bool = True) -> np.ndarray:
    """Features using one shared extractor, built on first use."""
    global _default
    if _default is None:
        _default = FbankExtractor()
    return _default.extract(audio, cmn=cmn)
