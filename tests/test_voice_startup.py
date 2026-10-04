"""The voice pipeline's startup ordering and buffering.

Written after a forensic investigation of a reported failure: "Hello NOVA"
produced no response, then a short cracked fragment arrived badly delayed.
Two independent defects caused it, and these tests pin both so they cannot
return.
"""
from __future__ import annotations

import io
import queue

import numpy as np
import pytest

from desk import live_session as ls


# -- ordering: consumers before producers ------------------------------------

def test_the_audio_readers_start_before_the_microphone():
    """The bug: the mic was opened and the greeting awaited before any task
    drained the mic queue. The queue holds 3.2 seconds, and dropped everything
    after that, so the user's first utterance was lost or sent stale."""
    src = io.open("desk/live_session.py", encoding="utf-8").read()
    body = src[src.index("async def _run_session"):] if "_run_session" in src else src
    block = body[body.index("LiveState.STREAMING"):body.index("except asyncio.CancelledError")]

    mic_sender = block.index("_mic_sender")
    start_mic = block.index("self._start_mic()")
    assert mic_sender < start_mic, (
        "the microphone is started before anything consumes its queue")


def test_the_greeting_never_blocks_the_conversation():
    """It is dispatched as a task, not awaited: composing it can touch NOVA
    Core, and the user is already trying to talk."""
    src = io.open("desk/live_session.py", encoding="utf-8").read()
    block = src[src.index("_has_greeted = True"):]
    block = block[:200]
    assert "create_task(self._send_greeting" in block, \
        "the greeting is awaited inline and can stall the session"
    assert "await self._send_greeting" not in block


def test_the_opening_line_runs_off_the_event_loop():
    """opening_line() lazily imports nova, which imports google.genai --
    measured at 7.4 seconds. Run on the loop it freezes the whole session."""
    src = io.open("desk/live_session.py", encoding="utf-8").read()
    fn = src[src.index("async def _send_greeting"):]
    fn = fn[:fn.index("\n    async def ", 10)] if "\n    async def " in fn[10:] else fn[:3000]
    assert "asyncio.to_thread" in fn, "opening_line still runs on the event loop"
    assert "wait_for" in fn, "a slow greeting has no deadline"


def test_the_greeting_has_a_bounded_budget():
    assert 0 < ls.GREETING_BUDGET_S <= 10


# -- the microphone queue ----------------------------------------------------

def test_a_full_mic_queue_drops_the_oldest_audio_not_the_newest():
    """A live conversation is not a recording. When the queue backs up, the
    frames worth keeping are the ones just spoken."""
    src = io.open("desk/live_session.py", encoding="utf-8").read()
    # Anchored on the callback itself rather than on whatever line happens to
    # follow it. The previous anchor was the exact indentation of the line
    # before sd.InputStream, so adding one line above it broke this test
    # without anything about the behaviour having changed.
    cb = src[src.index("def _cb(indata"):src.index("sd.InputStream(")]
    assert "get_nowait" in cb, "the callback still discards the newest frame"
    assert "_mic_dropped" in cb, "dropped frames are not counted"


def test_dropped_frames_are_reported_rather_than_hidden():
    src = io.open("desk/live_session.py", encoding="utf-8").read()
    stop = src[src.index("def _stop_mic"):]
    assert "_mic_dropped" in stop[:400]


# -- playback ----------------------------------------------------------------

def test_playback_does_not_request_the_smallest_possible_buffer():
    """latency="low" with blocksize=4096 blocked 6 of 12 writes past 150 ms on
    the test machine, against 0 of 12 for the defaults. Model audio arrives in
    bursts over a network, so playback needs slack."""
    src = io.open("desk/live_session.py", encoding="utf-8").read()
    stream = src[src.index("sd.RawOutputStream("):]
    stream = stream[:stream.index(")")]
    assert 'latency="low"' not in stream, "playback still asks for a tiny buffer"
    assert "blocksize=4096" not in stream


def test_a_preroll_cushions_network_jitter():
    assert 100 <= ls.PREROLL_MS <= 500, \
        "the pre-roll should cushion jitter without feeling sluggish"


def test_barge_in_discards_audio_held_in_the_preroll():
    """Draining the queue alone leaves buffered audio to play after an
    interruption -- the 'speaks a fragment of the old response' behaviour that
    barge-in exists to prevent."""
    src = io.open("desk/live_session.py", encoding="utf-8").read()
    fn = src[src.index("def _barge_in"):]
    fn = fn[:fn.index("\n    def ", 10)]
    assert "_play_generation" in fn, "the pre-roll survives a barge-in"


def test_the_speaker_thread_honours_the_generation_counter():
    src = io.open("desk/live_session.py", encoding="utf-8").read()
    worker = src[src.index("def _start_playback"):src.index("def _stop_playback")]
    assert worker.count("_play_generation") >= 2, \
        "the speaker thread does not notice playback being cut"


# -- instrumentation ---------------------------------------------------------

def test_the_trace_records_stage_boundaries_not_every_chunk():
    t = ls.VoiceTrace("t1")
    t.mark("connect_start")
    t.mark("connected")
    t.mark("mic_ready")
    assert set(t.stages) == {"connect_start", "connected", "mic_ready"}


def test_the_trace_reports_gaps_between_stages():
    t = ls.VoiceTrace("t2")
    t.mark("connect_start")
    t.stages["connected"] = t.stages["connect_start"] + 1.5
    assert round(t.gap("connect_start", "connected")) == 1500


def test_a_missing_stage_yields_no_gap_rather_than_a_wrong_one():
    t = ls.VoiceTrace("t3")
    t.mark("connect_start")
    assert t.gap("connect_start", "playback_started") is None
    assert "connect_start->connected" not in t.summary()


def test_only_the_first_occurrence_of_a_stage_is_kept():
    """A stage that re-marks would make every latency measurement wrong."""
    t = ls.VoiceTrace("t4")
    t.mark("connected")
    first = t.stages["connected"]
    t.mark("connected")
    assert t.stages["connected"] == first


def test_turn_timing_is_safe_before_a_turn_starts():
    ls.VoiceTrace("t5").turn_timing("audible")     # must not raise


# -- the shared policy is still the only one ---------------------------------

def test_terminal_and_desktop_still_share_one_voice_policy():
    for path in ("desk/live_session.py", "live_extra.py"):
        src = io.open(path, encoding="utf-8").read()
        assert "nova_voice" in src, f"{path} no longer uses the shared policy"


def test_the_audio_contract_is_unchanged():
    """16 kHz mono in, 24 kHz mono out. Changing either silently would be a
    far worse bug than the one being fixed."""
    import nova_voice
    assert nova_voice.SEND_RATE == 16000
    assert nova_voice.RECEIVE_RATE == 24000
    assert nova_voice.CHANNELS == 1
    assert ls.MIC_RATE == nova_voice.SEND_RATE
