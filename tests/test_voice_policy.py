"""The one voice behaviour model, pinned.

These rules came from the terminal implementation (live_extra.NOVALive), which
is the proven reference. The desktop had none of them, which is why it
transcribed NOVA's own speech, could not be interrupted, and dropped its Live
socket with a 1011 keepalive timeout every minute.
"""
from __future__ import annotations

import time

import numpy as np
import pytest

from nova_voice import (
    BARGE_IN_CHUNKS, BARGE_IN_RMS, SPEAK_COOLDOWN_S, VoiceGate, drain, frame_rms,
)

CHUNK = 1024


def loud(n=CHUNK, amp=3000):
    """A frame that unambiguously contains speech."""
    return (np.ones(n) * amp).astype(np.int16)


def quiet(n=CHUNK, amp=5):
    return (np.ones(n) * amp).astype(np.int16)


def gate(**kw):
    return VoiceGate(chunk_samples=CHUNK, **kw)


# ── listening ────────────────────────────────────────────────────────────────

def test_real_audio_is_sent_while_listening():
    g = gate()
    f = loud()
    assert g.process(f) == f.tobytes()
    assert g.frames_sent == 1


def test_quiet_audio_is_still_sent_while_listening():
    """The gate must not do its own VAD — the model handles turn detection."""
    g = gate()
    f = quiet()
    assert g.process(f) == f.tobytes()


# ── muting while NOVA speaks ─────────────────────────────────────────────────

def test_silence_is_sent_while_speaking():
    g = gate()
    g.set_speaking(True)
    out = g.process(quiet())
    assert out == bytes(CHUNK * 2)
    assert g.frames_muted == 1


def test_silence_is_never_none():
    """Withholding frames is what kills the Live socket (1011 keepalive)."""
    g = gate()
    g.set_speaking(True)
    assert g.process(quiet()) is not None
    g.set_muted(True)
    assert g.process(quiet()) is not None


def test_cooldown_keeps_mic_muted_just_after_speech():
    g = gate()
    g.set_speaking(True)
    g.set_speaking(False)          # stops now; cooldown begins
    assert g.process(quiet()) == bytes(CHUNK * 2)


def test_mic_reopens_after_cooldown():
    g = gate(cooldown_s=0.05)
    g.set_speaking(True)
    g.set_speaking(False)
    time.sleep(0.08)
    f = loud()
    assert g.process(f) == f.tobytes()


# ── barge-in ─────────────────────────────────────────────────────────────────

def test_barge_in_requires_sustained_speech():
    fired = []
    g = gate(on_barge_in=lambda: fired.append(1))
    g.set_speaking(True)
    g.process(loud())                      # run 1 — not yet
    assert fired == []
    g.process(loud())                      # run 2 — threshold met
    assert fired == [1]


def test_barge_in_sends_real_audio_so_the_model_also_stops():
    g = gate()
    g.set_speaking(True)
    g.process(loud())
    f = loud()
    assert g.process(f) == f.tobytes()


def test_barge_in_clears_speaking_state():
    g = gate()
    g.set_speaking(True)
    g.process(loud()); g.process(loud())
    assert g.speaking is False


def test_isolated_noise_does_not_interrupt():
    """A single loud frame between quiet ones must not cut NOVA off."""
    fired = []
    g = gate(on_barge_in=lambda: fired.append(1))
    g.set_speaking(True)
    for _ in range(6):
        g.process(loud())
        g.process(quiet())                 # breaks the run
    assert fired == []


def test_quiet_audio_never_barges_in():
    fired = []
    g = gate(on_barge_in=lambda: fired.append(1))
    g.set_speaking(True)
    for _ in range(20):
        g.process(quiet())
    assert fired == []


def test_no_barge_in_while_merely_in_cooldown():
    """Cooldown mutes, but NOVA is not speaking — nothing to interrupt."""
    fired = []
    g = gate(on_barge_in=lambda: fired.append(1))
    g.set_speaking(True)
    g.set_speaking(False)
    g.process(loud()); g.process(loud())
    assert fired == []


def test_barge_in_callback_failure_does_not_break_the_mic():
    def boom():
        raise RuntimeError("playback already gone")
    g = gate(on_barge_in=boom)
    g.set_speaking(True)
    g.process(loud())
    g.process(loud())                      # must not propagate
    assert g.process(loud()) is not None


# ── explicit mute ────────────────────────────────────────────────────────────

def test_user_mute_sends_silence_even_while_listening():
    g = gate()
    g.set_muted(True)
    assert g.process(loud()) == bytes(CHUNK * 2)


def test_unmute_restores_streaming_immediately():
    g = gate()
    g.set_muted(True); g.process(loud())
    g.set_muted(False)
    f = loud()
    assert g.process(f) == f.tobytes()


def test_mute_defaults_to_off():
    assert gate().muted is False


# ── helpers ──────────────────────────────────────────────────────────────────

def test_frame_rms_matches_expected_scale():
    assert frame_rms(quiet(amp=5)) < BARGE_IN_RMS
    assert frame_rms(loud(amp=3000)) > BARGE_IN_RMS
    assert frame_rms(np.array([], dtype=np.int16)) == 0.0


def test_drain_empties_a_thread_queue():
    import queue
    q = queue.Queue()
    for i in range(5):
        q.put(i)
    assert drain(q) == 5
    assert q.empty()


def test_drain_handles_none_and_mixed_queues():
    import queue
    q = queue.Queue()
    q.put(1)
    assert drain(None, q) == 1


def test_snapshot_reports_real_counters():
    g = gate()
    g.process(loud())
    g.set_speaking(True)
    g.process(quiet())
    s = g.snapshot()
    assert s["frames_sent"] == 1 and s["frames_muted"] == 1
    assert s["speaking"] is True and s["muted"] is False


def test_policy_constants_match_the_terminal():
    assert BARGE_IN_RMS == 350.0
    assert BARGE_IN_CHUNKS == 2
    assert SPEAK_COOLDOWN_S == 0.25


# ── session-level: NOVA must not interrupt herself ───────────────────────────

def _manager():
    """A LiveManager with no real audio devices or session attached."""
    from desk.live_session import LiveManager
    m = LiveManager()
    m._publish = lambda *a, **k: None          # no subscribers in a unit test
    return m


def test_barge_in_latches_so_the_rest_of_the_turn_is_dropped():
    """Regression: each arriving chunk re-armed 'speaking', the speaker echo
    tripped the detector again, and NOVA interrupted herself several times a
    second. Measured before the fix: ~5 barge-ins/sec; after: 1 in 45s."""
    m = _manager()
    m._enqueue_audio(b"\x00\x01" * 100)
    assert m._gate.speaking is True

    m._barge_in()
    assert m._interrupted_turn is True
    assert m._play_q.qsize() == 0, "queued audio must be dropped"

    m._enqueue_audio(b"\x00\x01" * 100)
    assert m._gate.speaking is False, "must not re-arm inside an interrupted turn"
    assert m._play_q.qsize() == 0


def test_a_new_turn_clears_the_latch():
    m = _manager()
    m._barge_in()
    assert m._interrupted_turn is True
    m._interrupted_turn = False                 # what turn_complete/interrupted do
    m._enqueue_audio(b"\x00\x01" * 100)
    assert m._gate.speaking is True


def test_mute_is_off_by_default_and_toggles():
    m = _manager()
    assert m.muted is False
    assert m.set_muted(True)["muted"] is True
    assert m.muted is True
    assert m.set_muted(False)["muted"] is False


def test_manager_uses_the_shared_policy():
    """Both surfaces must share one behaviour model, not two copies."""
    import nova_voice
    m = _manager()
    assert isinstance(m._gate, nova_voice.VoiceGate)
