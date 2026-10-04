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
    NO_ECHO_BARGE_IN_CHUNKS,
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
    """The full duplex policy — echo cancellation and automatic barge-in.

    Asked for explicitly, because it is no longer the default: NOVA ships the
    simple half-duplex policy while the basics are being made solid. This file
    is the full policy's contract, so it pins the full policy.
    """
    kw.setdefault("simple", False)
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



#: These fixtures hand the gate no playback reference, so there is no echo
#: path and the headphone rule applies: without NOVA's own voice to subtract,
#: the detector needs to watch the sound persist before believing a person is
#: behind it. The behaviour under test is unchanged — "sustained, not a
#: spike" — only how much counts as sustained.
SUSTAINED = NO_ECHO_BARGE_IN_CHUNKS

# ── barge-in ─────────────────────────────────────────────────────────────────

def test_barge_in_requires_sustained_speech():
    fired = []
    g = gate(on_barge_in=lambda: fired.append(1))
    g.set_speaking(True)
    for _ in range(SUSTAINED - 1):
        g.process(loud())
        assert fired == [], "interrupted before the evidence was in"
    g.process(loud())
    assert fired == [1]


def test_barge_in_sends_real_audio_so_the_model_also_stops():
    g = gate()
    g.set_speaking(True)
    for _ in range(SUSTAINED - 1):
        g.process(loud())
    f = loud()
    assert g.process(f) == f.tobytes()


def test_barge_in_clears_speaking_state():
    g = gate()
    g.set_speaking(True)
    for _ in range(SUSTAINED):
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
    for _ in range(SUSTAINED + 3):
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


def test_the_next_turn_plays_after_an_interruption():
    """Regression, and the opposite of the bug this file used to assert.

    There was a latch here that discarded every remaining chunk of an
    interrupted turn. It existed to stop playback resuming into a detector
    that was firing on NOVA’s own echo — and when it failed to clear, NOVA
    went silent for the rest of the session while the model went on replying.

    The tail of a cut turn is discarded again, because measurement against a
    real session showed the model goes on sending for over two seconds after
    a barge-in and every chunk of it was played. The difference is that the
    discarding now ends: the model confirming the cut closes the window, and
    a deadline closes it even if no confirmation ever arrives. Whatever the
    model says next has to be audible.
    """
    m = _manager()
    m._enqueue_audio(b"\x00\x01" * 100)
    assert m._gate.speaking is True

    m._barge_in()
    assert m._play_q.qsize() == 0, "queued audio must be dropped on barge-in"

    # What the receiver does when the model acknowledges the cut.
    m._end_suppression("model confirmed the cut")

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
    g = VoiceGate(chunk_samples=CHUNK, simple=False)
    g.set_speaking(True)
    g.set_speaking(False, interrupted=True)
    assert g._last_speak_end == 0.0, "an interruption armed the cooldown"


def test_a_natural_finish_arms_the_cooldown():
    g = VoiceGate(chunk_samples=CHUNK, simple=False)
    g.set_speaking(True)
    g.set_speaking(False)
    assert g._last_speak_end > 0.0, "a natural finish did not arm the cooldown"


# ── barge-in without an echo path ────────────────────────────────────────────

def _talk_over(coupling, room_rms, with_user, seed=3):
    """Play NOVA's recorded voice, optionally with a person talking over it.

    Returns milliseconds from the user starting to speak until barge-in, or
    None if it never fired.
    """
    import pathlib
    import wave

    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures" / "voice"
    nova_wav, user_wav = fixtures / "nova_speech_24k.wav", fixtures / "user_speech_16k.wav"
    if not (nova_wav.exists() and user_wav.exists()):
        return "skip"

    def read(p):
        with wave.open(str(p), "rb") as w:
            return (np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16),
                    w.getframerate())

    nova24, rate = read(nova_wav)
    user, _ = read(user_wav)
    idx = (np.arange(int(len(nova24) * 16000 / rate)) * rate / 16000).astype(np.int64)
    nova16 = nova24[idx[idx < len(nova24)]]

    g = VoiceGate(chunk_samples=CHUNK, simple=False)
    g.set_speaking(True)
    fired = []
    g._on_barge_in = lambda: fired.append(1)
    rng = np.random.default_rng(seed)
    user_at = 15

    for i in range(len(nova16) // CHUNK):
        seg = nova24[i * CHUNK * rate // 16000:(i + 1) * CHUNK * rate // 16000]
        if seg.size:
            g.reference(seg.tobytes(), rate)
        mic = rng.normal(0, room_rms, CHUNK)
        if coupling > 0 and i >= 3:
            echo = nova16[(i - 3) * CHUNK:(i - 2) * CHUNK]
            if echo.size == CHUNK:
                mic = mic + echo.astype(np.float64) * coupling
        if with_user and i >= user_at:
            k = (i - user_at) * CHUNK
            s = user[k:k + CHUNK]
            if s.size == CHUNK:
                mic = mic + s.astype(np.float64)
        g.process(np.clip(mic, -32768, 32767).astype(np.int16))
        if fired:
            return (i - user_at) * 64
    return None


def test_barge_in_works_with_no_echo_path_at_all():
    """Headphones, a distant speaker, or a quiet room.

    The canceller can never converge when NOVA's voice does not reach the
    microphone, because there is nothing to converge on. Requiring
    convergence therefore left her permanently uninterruptible on exactly
    those machines — reproduced at room tone RMS 700 with the user talking
    over her for four seconds and barge-in never firing once.
    """
    for room in (40, 300, 700):
        got = _talk_over(0.0, room, with_user=True)
        if got == "skip":
            return
        assert got is not None, f"uninterruptible with no echo path, room {room}"


def test_barge_in_is_quick_enough_to_feel_like_conversation():
    got = _talk_over(0.15, 40, with_user=True)
    if got == "skip":
        return
    assert got is not None
    assert got <= 400, f"took {got} ms to notice the user talking over her"


def test_no_self_interruption_across_rooms_and_couplings():
    """The invariant that must survive every change to the above."""
    for coupling, room in ((0.0, 700), (0.05, 700), (0.15, 40), (0.25, 300),
                           (0.40, 200), (0.60, 40), (0.10, 150)):
        got = _talk_over(coupling, room, with_user=False)
        if got == "skip":
            return
        assert got is None, (
            f"NOVA interrupted herself at {got} ms with {coupling:.0%} "
            f"coupling in a room at RMS {room}")


# ── the speaking flag is a state, not a heartbeat ────────────────────────────

def _barge_ins_over(frames: int, *, restate_each_frame: bool) -> int:
    """Talk over NOVA for `frames`, optionally re-asserting that she is speaking.

    `restate_each_frame` reproduces what the playback path does: it learns
    NOVA is speaking from each chunk of model audio it is handed, so it says
    so hundreds of times a turn rather than once.
    """
    rng = np.random.default_rng(7)
    g = gate()
    g.set_speaking(True)
    for _ in range(frames):
        if restate_each_frame:
            g.set_speaking(True)
        g.process(rng.normal(0, 6000, CHUNK).astype(np.int16))
    return g.barge_ins


def test_restating_speaking_does_not_reset_barge_in_progress():
    """The bug that made NOVA interruptible only by the button.

    Barge-in requires a run of consecutive speech frames — two with an echo
    path to measure against, sixteen without one (headphones). Everything
    `set_speaking(True)` clears is that run and the floor it is judged
    against, so re-running the reset between microphone frames meant the run
    restarted before it could ever complete. Measured before the fix: 60
    frames of unambiguous continuous speech, zero barge-ins.
    """
    assert _barge_ins_over(60, restate_each_frame=True) > 0, (
        "NOVA cannot be interrupted by voice while she is speaking")


def test_the_speaking_flag_is_idempotent():
    """Same speech, same outcome, however often the flag is re-asserted."""
    assert (_barge_ins_over(20, restate_each_frame=True)
            == _barge_ins_over(20, restate_each_frame=False))


def test_restating_not_speaking_does_not_extend_the_mic_cooldown():
    """A cooldown starts when speech ends. It does not restart on every poll.

    The cooldown exists to cover the speaker's decaying tail, so it is
    anchored to the moment NOVA stopped. Re-arming it from a caller that
    merely repeats "not speaking" would hold the microphone shut for as long
    as anything kept saying so.
    """
    g = gate()
    g.set_speaking(True)
    g.set_speaking(False)
    time.sleep(SPEAK_COOLDOWN_S + 0.05)
    g.set_speaking(False)
    f = loud()
    assert g.process(f) == f.tobytes(), "the cooldown was re-armed after it expired"
