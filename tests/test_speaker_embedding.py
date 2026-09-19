"""The model, and the numpy front end that feeds it.

WeSpeaker's ResNet34 was trained on Kaldi's 80-bin log-mel fbank, and it wants
exactly that. Features that are merely close do not degrade gracefully: they
come from a distribution the network has never seen, and every voice then
looks equally like every other. That failure is invisible from the shapes —
the embeddings still have 256 numbers and still normalise to one — so the
only honest check is whether real speech actually separates.

The reference front end is torchaudio's, which means torch, and NOVA runs this
on onnxruntime specifically so the packaged application does not carry torch
to tell two people apart. So the front end is reimplemented in numpy and
checked here against real audio instead of against torch.

The fixtures are two Windows voices saying two sentences each, two seconds
apiece. Synthetic voices are further apart than two people in one room, so
these thresholds are a floor on quality and not a measurement of it.
"""
from __future__ import annotations

import itertools
import wave
from pathlib import Path

import numpy as np
import pytest

from nova_identity import embedder as E
from nova_identity.fbank import FbankExtractor, fbank

FIXTURES = Path(__file__).parent / "fixtures" / "voices"
needs_model = pytest.mark.skipif(
    not E.available(),
    reason="speaker model not installed — run tools/fetch_speaker_model.py")


def read(name: str) -> np.ndarray:
    with wave.open(str(FIXTURES / f"{name}.wav"), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


# ── features ─────────────────────────────────────────────────────────────────

def test_frame_geometry_is_kaldis():
    """25 ms windows every 10 ms, FFT rounded up to a power of two. These are
    not choices; they are what the model was trained through."""
    f = FbankExtractor()
    assert (f.frame_length, f.frame_shift, f.n_fft) == (400, 160, 512)
    assert f.num_mel_bins == 80


def test_two_seconds_gives_the_expected_number_of_frames():
    feats = fbank(read("David_0"))
    # snip_edges: 1 + (32000 - 400) // 160
    assert feats.shape == (198, 80)


def test_features_are_mean_normalised_over_time():
    """Cepstral mean normalisation is what makes the embedding describe the
    speaker rather than the microphone and the room."""
    feats = fbank(read("David_0"), cmn=True)
    assert np.allclose(feats.mean(axis=0), 0.0, atol=1e-4)


def test_without_normalisation_the_mean_is_not_zero():
    """Guards the flag itself: if cmn were ignored the test above would pass
    for the wrong reason."""
    feats = fbank(read("David_0"), cmn=False)
    assert np.abs(feats.mean()) > 1e-3


def test_silence_does_not_produce_infinities():
    """Kaldi floors the log so a silent frame is a large negative number
    rather than -inf, which would poison everything downstream."""
    feats = fbank(np.zeros(16000, dtype=np.int16))
    assert np.all(np.isfinite(feats))


def test_integer_and_float_audio_agree():
    """Callers hand over whatever their capture path produced."""
    ints = read("David_0")
    floats = ints.astype(np.float32) / 32768.0
    assert np.allclose(fbank(ints), fbank(floats), atol=1e-3)


# ── embeddings ───────────────────────────────────────────────────────────────

@needs_model
def test_an_embedding_is_256_numbers_of_unit_length():
    v = E.SpeakerEmbedder.shared().embed(read("David_0"))
    assert v.shape == (E.EMBEDDING_DIM,)
    assert abs(float(np.linalg.norm(v)) - 1.0) < 1e-4


@needs_model
def test_the_same_audio_embeds_the_same_way_twice():
    e = E.SpeakerEmbedder.shared()
    a, b = e.embed(read("Zira_0")), e.embed(read("Zira_0"))
    assert E.similarity(a, b) > 0.9999


@needs_model
def test_audio_too_short_to_identify_a_voice_is_refused():
    """Half a second of speech does not describe a person. Returning a
    confident-looking vector computed from nothing is the worse answer."""
    e = E.SpeakerEmbedder.shared()
    with pytest.raises(ValueError, match="too short"):
        e.embed(read("David_0")[:8000])


@needs_model
def test_the_wrong_sample_rate_is_refused_rather_than_resampled():
    e = E.SpeakerEmbedder.shared()
    with pytest.raises(ValueError, match="16000"):
        e.embed(read("David_0"), rate=44100)


# ── the one that matters ─────────────────────────────────────────────────────

@needs_model
def test_real_speech_separates_by_speaker():
    """The end-to-end check on the numpy front end.

    Measured when this was written: same speaker +0.78..+0.91, different
    speaker +0.17..+0.26. A broken feature pipeline collapses that gap while
    leaving every shape and norm looking perfectly correct.
    """
    e = E.SpeakerEmbedder.shared()
    vecs = {n: e.embed(read(n)) for n in
            ("David_0", "David_1", "Zira_0", "Zira_1")}

    same = [E.similarity(vecs["David_0"], vecs["David_1"]),
            E.similarity(vecs["Zira_0"], vecs["Zira_1"])]
    diff = [E.similarity(vecs[a], vecs[b])
            for a, b in itertools.product(("David_0", "David_1"),
                                          ("Zira_0", "Zira_1"))]

    assert min(same) > 0.60, f"one voice did not match itself: {same}"
    assert max(diff) < 0.45, f"two voices were not told apart: {diff}"
    assert min(same) - max(diff) > 0.20, (
        f"margin too thin: same={same}, different={diff}")


@needs_model
def test_the_shipped_thresholds_sit_inside_the_measured_gap():
    """A threshold outside the gap is either a door that never opens or one
    that never closes."""
    from nova_identity import profiles as P
    e = E.SpeakerEmbedder.shared()
    same = E.similarity(e.embed(read("David_0")), e.embed(read("David_1")))
    diff = E.similarity(e.embed(read("David_0")), e.embed(read("Zira_0")))
    assert diff < P.PROBABLE < P.ACCEPT < same
