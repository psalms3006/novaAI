"""NOVA must never stop hearing the user.

The reported failure was that NOVA answered the first question and was silent
for every one after it — and about seventy seconds later the connection died.
Both halves of that had the same cause, and it was not the microphone:

`session.receive()` in google-genai yields **one model turn** and then the
generator ends. A bare `async for msg in session.receive()` therefore reads
exactly one reply and falls off the end. NOVA then never read another message,
so she never spoke again and never noticed anything said to her; and because
an unread WebSocket is an unread transport, the library's incoming queue
filled, pongs stopped being processed, and the socket was closed with
"keepalive ping timeout".

These tests pin the loop that fixes it, and the surrounding rules that keep a
muted microphone from becoming a permanent one.
"""
from __future__ import annotations

import inspect
import queue as q
import threading

import numpy as np

import nova_voice
from desk import live_session as ls

CHUNK = b"\x10\x00" * 512


def session():
    """A LiveManager with the audio hardware stubbed out."""
    m = ls.LiveManager.__new__(ls.LiveManager)
    m._play_q = q.Queue(maxsize=200)
    m._subscribers = []
    m._subs_lock = threading.Lock()
    m._speaker_alive = True
    m._last_audio_at = 0.0
    m._play_generation = 0
    m._trace = None
    m._turn_done_flag = False
    m._turn_count = 0
    m._audio_bytes_out = 0
    m._gate = nova_voice.VoiceGate(chunk_samples=1024, on_barge_in=m._barge_in)
    m._stream = None                # no real output device in a unit test
    m._stream_lock = threading.Lock()
    m._playing_until = 0.0
    m._published = []
    m._publish = lambda ev: m._published.append(ev)
    return m


# ── the receive loop ─────────────────────────────────────────────────────────

def test_the_receiver_reads_more_than_one_turn():
    """The whole bug, in one assertion.

    `receive()` breaks on turn_complete. Without an outer loop the receiver
    coroutine returns after NOVA's first reply and the session is finished as
    a conversation, whatever the microphone is doing.
    """
    src = inspect.getsource(ls.LiveManager._receiver)
    body = src.split('"""')[-1]                  # skip the docstring
    for_at = body.index("async for msg in session.receive()")
    assert "while True" in body[:for_at], (
        "the receiver reads a single turn; session.receive() is one turn, "
        "not the session")


def test_the_receiver_does_not_spin_when_the_stream_ends():
    """An outer loop around a generator that returns nothing is a busy loop."""
    src = inspect.getsource(ls.LiveManager._receiver)
    assert "got_turn" in src and "raise ConnectionError" in src, (
        "nothing stops the receive loop when the socket goes away")


# ── audio must resume ────────────────────────────────────────────────────────

def test_barge_in_on_the_tail_does_not_deafen_the_next_turn():
    """The user cuts in just as NOVA finishes, so the model has already sent
    turn_complete and never sends `interrupted`. The next turn must be
    audible."""
    m = session()
    m._enqueue_audio(CHUNK)
    m._turn_done_flag = True                # turn_complete arrived
    m._barge_in()

    before = m._play_q.qsize()
    m._enqueue_audio(CHUNK)
    assert m._play_q.qsize() > before, "NOVA went deaf: turn 2 audio was dropped"


def test_audio_resumes_immediately_after_an_interruption():
    """There is deliberately no suppression latch any more.

    There used to be one: after a barge-in the rest of that turn was
    discarded, because the detector was firing on NOVA's own echo and playback
    had to be kept from re-triggering it. Its failure mode was NOVA going
    silent for the remainder of the session while the model went on replying.
    The echo is cancelled now, so an interruption means the user really spoke,
    the model is told, and it stops on its own.
    """
    m = session()
    m._enqueue_audio(CHUNK)
    m._barge_in()
    assert m._play_q.qsize() == 0, "queued audio should be dropped on barge-in"

    for _ in range(5):
        m._enqueue_audio(CHUNK)
    assert m._play_q.qsize() == 5, "NOVA stayed mute after being interrupted"


def test_ten_consecutive_turns_stay_audible():
    m = session()
    for turn in range(10):
        m._enqueue_audio(CHUNK)
        assert m._play_q.qsize() > 0, f"turn {turn} produced no audio"
        while not m._play_q.empty():
            m._play_q.get_nowait()
        m._turn_done_flag = True
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


# ── the greeting must not deafen the session ─────────────────────────────────

def test_the_greeting_waits_for_the_microphone_stream():
    """Client content sent before the audio stream is established leaves
    Gemini Live never activating audio input for that session — the model
    answers the text, and every word the user says afterwards is ignored for
    the life of the connection. Measured directly against the API.
    """
    src = inspect.getsource(ls.LiveManager._send_greeting)
    assert "_await_mic_stream" in src, (
        "the greeting no longer waits for microphone audio to reach the "
        "model; it will deafen the session")


def test_the_microphone_warmup_is_a_real_amount_of_audio():
    assert ls.MIC_WARMUP_BYTES >= ls.MIC_RATE          # at least 0.5 s
    assert 0 < ls.MIC_WARMUP_WAIT_S <= 10


def test_the_greeting_gives_up_rather_than_staying_silent_forever():
    """A microphone that never produces anything is a broken microphone, not
    a reason for NOVA to never speak."""
    src = inspect.getsource(ls.LiveManager._await_mic_stream)
    assert "return False" in src and "deadline" in src
