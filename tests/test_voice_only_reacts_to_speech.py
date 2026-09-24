"""NOVA reacts to a voice, not to sound.

Observed 2026-09-24 12:29-12:35: her greeting (two sentences) was cut after
one by a barge-in with nobody talking, and the first answer was cut five
seconds in. Two faults, one root: every decision about "the user is talking"
was made on loudness.

* Barge-in fired on anything sufficiently louder than the room -- a key, a
  chair, a knock -- so she stopped mid-sentence on noise.
* While she was quiet, everything above room level (and the room itself, as
  periodic keep-alives) was streamed to Gemini, whose own detector then
  heard "speech" in noise and answered it.

Both now go through a speech detector (Silero VAD, the model faster-whisper
ships): only speech interrupts her, only speech -- plus extremely loud
events, which are worth her attention but never interrupt her -- reaches the
model, and everything else is sent as true silence.
"""
from __future__ import annotations

import asyncio
import pathlib
import threading
import time
import wave
from types import SimpleNamespace

import numpy as np
import pytest

import nova_voice
from nova_voice import SpeechDetector, VoiceGate

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "voice"
FRAME = 1024


def _read(path):
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16), w.getframerate()


def _user_speech(rms=4500.0):
    pcm, rate = _read(FIXTURES / "user_speech_16k.wav")
    assert rate == 16000
    x = pcm.astype(np.float64)
    return x / (np.sqrt(np.mean(x ** 2)) + 1e-9) * rms


def _nova_ref():
    pcm, rate = _read(FIXTURES / "nova_speech_24k.wav")
    return pcm, rate


def _room(n, rms=1500.0, seed=0):
    return np.random.default_rng(seed).standard_normal(n) * rms


def _clicks(n, seed=1):
    """Keyboard and desk: short sharp transients, far above the room."""
    rng = np.random.default_rng(seed)
    x = np.zeros(n)
    for k in range(0, n, 3000):
        x[k:k + 60] += rng.standard_normal(min(60, n - k)) * 18000
    return x


def _bang(n, at, seed=2):
    """A door slam: ~0.3 s far louder than anything else in the room."""
    x = np.zeros(n)
    rng = np.random.default_rng(seed)
    x[at:at + 4800] = rng.standard_normal(min(4800, n - at)) * 16000 * np.exp(
        -np.arange(min(4800, n - at)) / 1500)
    return x


def _frames(x):
    x = np.clip(x, -32768, 32767).astype(np.int16)
    return [x[i:i + FRAME] for i in range(0, len(x) - FRAME + 1, FRAME)]


# ── the detector itself ──────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def detector():
    d = SpeechDetector()
    assert d.available, (
        "the speech model could not be loaded -- without it NOVA falls back "
        "to reacting to loudness")
    return d


def _voiced_share(det, x):
    det.reset()
    probs = [det.probability(f) for f in _frames(x)]
    return float(np.mean(np.array(probs) >= nova_voice.VOICE_PROB))


def test_speech_is_recognised_over_the_room(detector):
    speech = _user_speech()
    assert _voiced_share(detector, speech + _room(len(speech))) > 0.4


@pytest.mark.parametrize("name,make", [
    ("room noise", lambda n: _room(n)),
    ("keyboard and desk clicks", lambda n: _room(n) + _clicks(n)),
    ("a door slam", lambda n: _room(n) + _bang(n, 8000)),
    ("mains hum", lambda n: _room(n) + np.sin(
        2 * np.pi * 100 * np.arange(n) / 16000) * 8000),
])
def test_sounds_that_are_not_speech_are_not_speech(detector, name, make):
    assert _voiced_share(detector, make(16000 * 4)) == 0.0, name


# ── barge-in: only a voice interrupts her ────────────────────────────────────

def _gate():
    g = VoiceGate(chunk_samples=FRAME, simple=False, speech_detector=SpeechDetector())
    for f in _frames(_room(16000 * 2, seed=9)):      # the room before she speaks
        g.process(f)
    return g


def _talk(g, mic):
    """NOVA speaks (into earbuds) while `mic` is what the microphone hears."""
    fired = []
    g._on_barge_in = lambda: fired.append(len(fired))
    ref, rate = _nova_ref()
    step = FRAME * rate // 16000
    g.set_speaking(True)
    at = []
    for i, f in enumerate(_frames(mic)):
        seg = ref[(i * step) % max(1, len(ref) - step):][:step]
        g.reference(seg.tobytes(), rate)
        before = len(fired)
        g.process(f)
        if len(fired) > before:
            at.append(i * FRAME / 16000)
            g.set_speaking(True)                     # her next sentence
    return at


def test_noise_never_cuts_her_off():
    n = 16000 * 12
    mic = _room(n) + _clicks(n) + _bang(n, 16000 * 5)
    assert _talk(_gate(), mic) == [], "NOVA stopped herself on noise"


def test_the_user_talking_over_her_still_stops_her():
    speech = _user_speech()
    lead = 16000 * 2
    mic = _room(lead + len(speech))
    mic[lead:] += speech
    at = _talk(_gate(), mic)
    assert at, "a person talking over her no longer interrupts her"
    assert at[0] - lead / 16000 < 1.5, f"interruption took {at[0] - 2:.2f}s"


# ── what reaches the model while she is quiet ────────────────────────────────

def test_the_gate_marks_voice_and_loud_events_not_noise():
    g = _gate()
    for f in _frames(_room(16000) + _clicks(16000)):
        g.process(f)
        assert not g.last_is_voice and not g.last_is_loud_event
    for f in _frames(_bang(16000, 0) + _room(16000))[:3]:
        g.process(f)
    assert g.last_is_loud_event, "an extremely loud sound should get her attention"
    assert not g.last_is_voice
    speech = _user_speech()
    voiced = 0
    for f in _frames(speech + _room(len(speech))):
        g.process(f)
        voiced += g.last_is_voice
    assert voiced > 0.5 * (len(speech) // FRAME)


def test_only_speech_is_uplinked_and_its_start_is_not_clipped():
    from desk.live_session import UplinkFilter
    uf = UplinkFilter(preroll_frames=3)
    raw = [bytes([i + 1]) * 2048 for i in range(6)]
    out = []
    for i in range(4):                                # room: nothing real goes up
        out += uf.push(raw[i], raw[i], send_real=False)
    assert all(p == bytes(2048) and silent for p, silent in out)
    onset = uf.push(raw[4], raw[4], send_real=True)   # speech begins
    assert [p for p, _ in onset] == raw[1:5], "the pre-roll must lead the frame"
    assert not any(s for _, s in onset)
    assert uf.push(raw[5], raw[5], send_real=True) == [(raw[5], False)]


class FakeInputStream:
    last = None

    def __init__(self, callback=None, **kw):
        self.callback = callback
        FakeInputStream.last = self

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass


def test_the_live_microphone_sends_silence_for_noise_and_audio_for_speech(monkeypatch):
    """End to end through the real mic path: callback, gate thread, filter,
    batcher, send queue."""
    from desk import live_session as ls
    monkeypatch.setattr(ls, "HAS_SD", True)
    monkeypatch.setattr(ls, "sd", SimpleNamespace(InputStream=FakeInputStream), raising=False)
    monkeypatch.setattr(ls, "_mic_device_resolved", lambda: None)
    monkeypatch.setattr(ls, "_mic_device", lambda: None)
    m = ls.LiveManager()
    m._publish = lambda ev: None
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        m._loop = loop
        m._mic_queue = asyncio.Queue(maxsize=1000)
        m._start_mic()
        cb = FakeInputStream.last.callback
        # Room, typing, and a desk bang far above everything else: measured
        # here, such bangs hit the laptop mic many times a minute, so they
        # must not reach the model either.
        n = 16000 * 3
        noise = _frames(_room(n, seed=4) + _clicks(n) + _bang(n, 16000))
        for f in noise:
            cb(f.reshape(-1, 1), FRAME, None, None)
            time.sleep(0.002)
        time.sleep(0.5)
        during_noise = []
        while not m._mic_queue.empty():
            during_noise.append(m._mic_queue.get_nowait())
        assert all(not any(b) for b in during_noise), (
            "room noise and clicks were streamed to the model")

        speech = _user_speech()
        for f in _frames(speech + _room(len(speech), seed=5)):
            cb(f.reshape(-1, 1), FRAME, None, None)
            time.sleep(0.002)
        time.sleep(0.5)
        during_speech = []
        while not m._mic_queue.empty():
            during_speech.append(m._mic_queue.get_nowait())
        assert any(any(b) for b in during_speech), "the user's speech never went up"
    finally:
        m._stop_mic()
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)
