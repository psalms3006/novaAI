"""The one voice behaviour model, pinned.

Every NOVA surface listens through :class:`nova_voice.VoiceGate`, so these are
the rules the desktop window, the ambient orb and the terminal all obey.

The barge-in rules here changed after NOVA was found interrupting herself on
every turn. The old policy was a fixed loudness threshold and it could not
work: a threshold low enough to hear a person is also low enough to hear
NOVA's own voice coming back through the speakers. What replaced it is
measured against real recordings in test_voice_gate_echo.py; this file pins
the surrounding contract — what gets transmitted, when the microphone is
muted, and what must never happen.
"""
from __future__ import annotations

import time

import numpy as np

from nova_voice import (
    BARGE_IN_CHUNKS,
    BARGE_IN_FLOOR_RMS,
    REF_TAIL_S,
    SPEAK_COOLDOWN_S,
    VoiceGate,
    drain,
    frame_rms,
)

CHUNK = 1024
SILENCE = bytes(CHUNK * 2)


def loud(n=CHUNK, amp=3000):
    """A frame that unambiguously contains speech."""
    rng = np.random.default_rng(0)
    return (rng.normal(0, amp, n)).astype(np.int16)


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
    assert g.process(quiet()) == SILENCE
    assert g.frames_muted == 1


def test_silence_is_never_none():
    """A gap in the stream is worse than silence: the model has to guess."""
    g = gate()
    g.set_speaking(True)
    assert g.process(quiet()) is not None
    g.set_muted(True)
    assert g.process(quiet()) is not None


def test_muted_frames_are_marked_as_silence():
    """The transport batches on this to avoid paying to transmit zeroes."""
    g = gate()
    g.process(loud())
    assert g.last_was_silence is False
    g.set_speaking(True)
    g.process(quiet())
    assert g.last_was_silence is True


def test_cooldown_keeps_mic_muted_just_after_speech():
    g = gate()
    g.set_speaking(True)
    g.set_speaking(False)          # stops now; cooldown begins
    assert g.process(quiet()) == SILENCE


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
    for _ in range(BARGE_IN_CHUNKS - 1):
        g.process(loud())
        assert fired == [], "interrupted before the evidence was in"
    g.process(loud())
    assert fired == [1]


def test_barge_in_sends_real_audio_so_the_model_also_stops():
    g = gate()
    g.set_speaking(True)
    for _ in range(BARGE_IN_CHUNKS - 1):
        g.process(loud())
    f = loud()
    assert g.process(f) == f.tobytes()


def test_barge_in_clears_speaking_state():
    g = gate()
    g.set_speaking(True)
    for _ in range(BARGE_IN_CHUNKS):
        g.process(loud())
    assert g.speaking is False


def test_one_loud_frame_does_not_interrupt():
    """A door, a cough, a single clipped syllable of NOVA's own voice."""
    fired = []
    g = gate(on_barge_in=lambda: fired.append(1))
    g.set_speaking(True)
    g.process(loud())
    for _ in range(20):
        g.process(quiet())
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
    for _ in range(BARGE_IN_CHUNKS + 2):
        g.process(loud())
    assert fired == []


def test_barge_in_callback_failure_does_not_break_the_mic():
    def boom():
        raise RuntimeError("playback already gone")
    g = gate(on_barge_in=boom)
    g.set_speaking(True)
    for _ in range(BARGE_IN_CHUNKS + 1):
        assert g.process(loud()) is not None      # must not propagate


def test_the_speaker_tail_cannot_be_mistaken_for_the_user():
    """The reference stops when audio is handed to the device, not when the
    room stops hearing it. NOVA's last words are still in the air for a few
    hundred milliseconds afterwards, with nothing left to cancel them against.

    Seen exactly once in an otherwise clean run: a barge-in fired on the tail
    of her greeting, and the model transcribed her own closing words back as
    something the user had said.
    """
    fired = []
    g = gate(on_barge_in=lambda: fired.append(1))
    g.set_speaking(True)
    # Playback: loud reference, then it stops being handed to the device.
    ref = (np.random.default_rng(1).normal(0, 6000, 24000)).astype(np.int16)
    g.reference(ref.tobytes(), 24000)
    g.process(quiet())
    # ...and now the tail of it arrives at the microphone.
    for _ in range(BARGE_IN_CHUNKS + 3):
        g.process(loud())
    assert fired == [], "interrupted by NOVA's own decaying tail"


def test_the_tail_guard_expires():
    """It suppresses a moment, not the rest of the turn."""
    g = gate()
    g.set_speaking(True)
    ref = (np.random.default_rng(1).normal(0, 6000, 24000)).astype(np.int16)
    g.reference(ref.tobytes(), 24000)
    g.process(quiet())
    time.sleep(REF_TAIL_S + 0.05)
    fired = []
    g._on_barge_in = lambda: fired.append(1)
    for _ in range(BARGE_IN_CHUNKS + 3):
        g.process(loud())
    assert fired == [1], "still deaf long after the speaker went quiet"


# ── explicit mute ────────────────────────────────────────────────────────────

def test_user_mute_sends_silence_even_while_listening():
    g = gate()
    g.set_muted(True)
    assert g.process(loud()) == SILENCE


def test_unmute_restores_streaming_immediately():
    g = gate()
    g.set_muted(True)
    g.process(loud())
    g.set_muted(False)
    f = loud()
    assert g.process(f) == f.tobytes()


def test_mute_defaults_to_off():
    assert gate().muted is False


# ── helpers ──────────────────────────────────────────────────────────────────

def test_frame_rms_matches_expected_scale():
    assert frame_rms(quiet(amp=5)) < BARGE_IN_FLOOR_RMS
    assert frame_rms(loud(amp=3000)) > BARGE_IN_FLOOR_RMS
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


def test_policy_constants_are_survivable():
    """Guard rails, not magic numbers.

    The floor has to sit far above room tone (~40) and far below a person
    talking at a laptop (~2500), and the old value of 350 was neither — it was
    below NOVA's own echo, which is the entire bug.
    """
    assert BARGE_IN_FLOOR_RMS > 350.0
    assert BARGE_IN_FLOOR_RMS < 2000.0
    assert BARGE_IN_CHUNKS >= 2
    assert SPEAK_COOLDOWN_S == 0.25


# ── session level ────────────────────────────────────────────────────────────

def _manager():
    """A LiveManager with no real audio devices or session attached."""
    from desk.live_session import LiveManager
    m = LiveManager()
    m._publish = lambda *a, **k: None          # no subscribers in a unit test
    m._speaker_alive = True                    # no real output device in tests
    return m


def test_audio_keeps_playing_after_an_interruption():
    """Regression, and the opposite of the bug this file used to assert.

    There was a latch here that discarded every remaining chunk of an
    interrupted turn. It existed to stop playback resuming into a detector
    that was firing on NOVA's own echo — and when it failed to clear, NOVA
    went silent for the rest of the session while the model went on replying.
    With the echo cancelled the latch is gone, and the next turn must play.
    """
    m = _manager()
    m._enqueue_audio(b"\x00\x01" * 100)
    assert m._gate.speaking is True

    m._barge_in()
    assert m._play_q.qsize() == 0, "queued audio must be dropped on barge-in"

    m._enqueue_audio(b"\x00\x01" * 100)
    assert m._gate.speaking is True, "NOVA stayed mute after being interrupted"
    assert m._play_q.qsize() == 1


def test_barge_in_does_not_arm_the_speaker_cooldown():
    """The user is mid-sentence; swallowing 250 ms of it loses the request."""
    m = _manager()
    m._enqueue_audio(b"\x00\x01" * 100)
    m._barge_in()
    f = loud()
    assert m._gate.process(f) == f.tobytes()


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


def test_playback_feeds_the_echo_canceller():
    """Without a reference the canceller has nothing to cancel, and the gate
    silently degrades to the loudness test that caused the original bug."""
    import inspect
    from desk import live_session
    src = inspect.getsource(live_session.LiveManager._start_playback)
    assert "self._gate.reference(" in src, (
        "the playback path no longer tells the voice gate what it played")


def test_terminal_playback_also_feeds_the_echo_canceller():
    import inspect
    import live_extra
    src = inspect.getsource(live_extra)
    assert "gate.reference(" in src, (
        "the terminal path stopped sharing the desktop's listening policy")


# ── the two endings ──────────────────────────────────────────────────────────

def test_an_interruption_does_not_arm_the_cooldown():
    g = VoiceGate(chunk_samples=CHUNK)
    g.set_speaking(True)
    g.set_speaking(False, interrupted=True)
    assert g._last_speak_end == 0.0, "an interruption armed the cooldown"


def test_a_natural_finish_arms_the_cooldown():
    g = VoiceGate(chunk_samples=CHUNK)
    g.set_speaking(True)
    g.set_speaking(False)
    assert g._last_speak_end > 0.0, "a natural finish did not arm the cooldown"
