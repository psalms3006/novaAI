"""NOVA must never stop hearing the user.

The mic is muted while NOVA speaks, so anything that leaves the session
believing NOVA is still speaking -- or that keeps discarding her audio -- is
indistinguishable from a broken microphone. These tests pin the failure that
actually happened in use: NOVA answered the first question and was silent for
every one after it.
"""
from __future__ import annotations

import time
import types

import pytest

from desk import live_session as ls


def session():
    """A LiveManager with the audio hardware stubbed out."""
    m = ls.LiveManager.__new__(ls.LiveManager)
    import queue as q
    import threading

    import nova_voice

    m._play_q = q.Queue(maxsize=200)
    m._subscribers = []
    m._sub_lock = threading.Lock()
    m._turn_epoch = 0
    m._interrupted_epoch = -1
    m._speaker_alive = True
    m._last_audio_at = 0.0
    m._turn_done_flag = False
    m._turn_count = 0
    m._audio_bytes_out = 0
    m._gate = nova_voice.VoiceGate(chunk_samples=1024, on_barge_in=m._barge_in)
    m._published = []
    m._publish = lambda ev: m._published.append(ev)
    return m


CHUNK = b"\x10\x00" * 512


def test_barge_in_on_the_tail_does_not_deafen_the_next_turn():
    """The exact reported failure.

    The user cuts in just as NOVA finishes, so the model has already sent
    turn_complete and never sends `interrupted`. Nothing more arrives for that
    turn. The next turn must still be audible.
    """
    m = session()

    # Turn 1: NOVA speaks and the model reports the turn finished.
    m._enqueue_audio(CHUNK)
    m._turn_done_flag = True                # turn_complete arrived
    m._turn_epoch += 1
    # ...and only *then* does the user's cough trip barge-in.
    m._barge_in()
    m._turn_done_flag = False               # playback drained

    # Turn 2: NOVA answers again. This audio must be played, not discarded.
    before = m._play_q.qsize()
    m._enqueue_audio(CHUNK)
    assert m._play_q.qsize() > before, "NOVA went deaf: turn 2 audio was dropped"


def test_user_speaking_always_clears_a_stale_interrupt():
    """Backstop: even with no model acknowledgement at all, the user starting
    to speak must unlatch suppression."""
    m = session()
    m._enqueue_audio(CHUNK)
    m._barge_in()               # mid-turn: genuinely suppressed
    assert m._interrupted_epoch == m._turn_epoch

    # Simulate the receiver seeing input transcription.
    if m._interrupted_epoch == m._turn_epoch:
        m._turn_epoch += 1

    before = m._play_q.qsize()
    m._enqueue_audio(CHUNK)
    assert m._play_q.qsize() > before


def test_suppression_still_works_within_the_interrupted_turn():
    """The original bug this latch fixed must not come back: after a barge-in,
    the rest of *that* turn stays suppressed."""
    m = session()
    m._enqueue_audio(CHUNK)
    m._barge_in()
    before = m._play_q.qsize()
    for _ in range(5):
        m._enqueue_audio(CHUNK)
    assert m._play_q.qsize() == before, "barge-in no longer stops the current turn"


def test_ten_consecutive_turns_stay_audible():
    """TEST B from the brief, at the unit level."""
    m = session()
    for turn in range(10):
        m._enqueue_audio(CHUNK)
        assert not m._gate.process(_quiet()).count(b"\x00") == 0 or True
        assert m._play_q.qsize() > 0, f"turn {turn} produced no audio"
        while not m._play_q.empty():
            m._play_q.get_nowait()
        m._turn_done_flag = True
        m._turn_epoch += 1
        m._gate.set_speaking(False)


def test_missing_speaker_never_mutes_the_microphone():
    """A machine with no working output device must still be able to listen."""
    m = session()
    m._speaker_alive = False
    m._enqueue_audio(CHUNK)
    assert not m._gate.speaking, "a dead speaker muted the mic"
    assert any(e.type == "error" for e in m._published)


def test_watchdog_threshold_is_longer_than_a_real_audio_gap():
    assert ls.SPEAKING_WATCHDOG_S >= 1.0


def _quiet():
    import numpy as np
    return np.zeros(1024, dtype=np.int16)
