"""The policy NOVA actually ships while the basics are being made solid.

Half-duplex, and deliberately so: while NOVA speaks the microphone is muted,
and the rest of the time every frame goes straight out untouched. No echo
canceller, no automatic barge-in, no learned floors.

This is the shipped reference implementation's design. It costs an
interruption button instead of interrupting by voice, and it buys a
microphone path with nothing in it that can go wrong. The full policy is not
deleted — test_voice_policy.py and test_voice_gate_echo.py still pin it, and
NOVA_VOICE_FULL_DUPLEX=1 turns it back on.
"""
from __future__ import annotations

import time

import numpy as np

import nova_voice
from nova_voice import SPEAK_COOLDOWN_S, VoiceGate

CHUNK = 1024
SILENCE = bytes(CHUNK * 2)


def loud(amp=6000):
    return np.random.default_rng(1).normal(0, amp, CHUNK).astype(np.int16)


def gate(**kw):
    kw.setdefault("simple", True)
    return VoiceGate(chunk_samples=CHUNK, **kw)


# ── what ships ───────────────────────────────────────────────────────────────

def test_the_simple_policy_is_the_default():
    assert VoiceGate(chunk_samples=CHUNK).simple is True


def test_the_full_policy_is_still_reachable(monkeypatch):
    """Not deleted — waiting for the basics to be solid underneath it."""
    monkeypatch.setenv("NOVA_VOICE_FULL_DUPLEX", "1")
    assert nova_voice.simple_voice_default() is False
    assert VoiceGate(chunk_samples=CHUNK).simple is False


# ── the policy ───────────────────────────────────────────────────────────────

def test_every_frame_is_transmitted_while_listening():
    """The whole point. Audio that is never transmitted cannot be understood,
    and that was the bug: NOVA could not hear because a quarter of the
    microphone was being thrown away before it reached the model."""
    g = gate()
    f = loud()
    assert g.process(f) == f.tobytes()
    assert g.frames_sent == 1


def test_quiet_audio_is_transmitted_too():
    """Turn detection is the model's job. A gate that decides what counts as
    speech is a gate that can decide wrongly."""
    g = gate()
    f = (np.ones(CHUNK) * 12).astype(np.int16)
    assert g.process(f) == f.tobytes()


def test_the_microphone_is_muted_while_NOVA_speaks():
    g = gate()
    g.set_speaking(True)
    assert g.process(loud()) == SILENCE


def test_silence_is_sent_rather_than_nothing():
    """Withholding frames entirely leaves gaps the model's turn detection has
    to guess about."""
    g = gate()
    g.set_speaking(True)
    assert len(g.process(loud())) == CHUNK * 2


def test_the_speaker_tail_is_not_mistaken_for_the_user():
    """A moment after she stops, her own decay is still reaching the room."""
    g = gate()
    g.set_speaking(True)
    g.set_speaking(False)
    assert g.process(loud()) == SILENCE


def test_the_tail_guard_lets_go():
    g = gate()
    g.set_speaking(True)
    g.set_speaking(False)
    time.sleep(SPEAK_COOLDOWN_S + 0.05)
    f = loud()
    assert g.process(f) == f.tobytes()


def test_a_muted_user_is_still_muted():
    g = gate()
    g.set_muted(True)
    assert g.process(loud()) == SILENCE


# ── and nothing expensive happens per frame ──────────────────────────────────

def test_nothing_is_spent_analysing_the_frame():
    """A PortAudio callback has a hard real-time budget: 64 ms of audio per
    block at this size. The operating system was already dropping blocks
    before NOVA saw them, so the policy must cost effectively nothing."""
    g = gate()
    g.set_speaking(True)
    f = loud()
    for _ in range(50):
        g.process(f)
    t = time.perf_counter()
    for _ in range(500):
        g.process(f)
    per_frame_ms = (time.perf_counter() - t) / 500 * 1000
    assert per_frame_ms < 1.0, f"{per_frame_ms:.2f} ms per frame in the callback"


def test_playback_is_not_buffered_for_a_canceller_that_is_off():
    """Filling the reference buffer means resampling every chunk of NOVA's own
    speech on the playback thread, for nobody."""
    g = gate()
    g.reference(b"\x10\x00" * 2400, 24000)
    assert g.echo._ref.size == 0


def test_the_full_policy_still_keeps_its_reference():
    g = gate(simple=False)
    g.reference(b"\x10\x00" * 2400, 24000)
    assert g.echo._ref.size > 0


def test_there_is_no_barge_in_under_the_simple_policy():
    """Stated so the trade is explicit rather than discovered. Interrupting is
    a button for now; it comes back with the full policy."""
    fired = []
    g = gate(on_barge_in=lambda: fired.append(1))
    g.set_speaking(True)
    for _ in range(80):
        g.process(loud())
    assert fired == []
    assert g.speaking is True
