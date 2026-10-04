"""Noticing that NOVA has gone quiet, instead of letting the user talk to nothing.

From a real session (2026-09-20 16:15-16:18):

    16:15:59  turn 8: model audio        <- last answer
    16:16:11  Memory extraction failed: 503 (Google under load)
    16:16:51  turn 9:  user speech detected
    16:16:57  turn 10: user speech detected
    16:16:59  turn 11: user speech detected
    16:17:28  turn 12: user speech detected
    16:18:07  loop ended on shutdown

Four turns, ninety seconds, no reply and no complaint. At teardown the
receiver task was still pending, blocked on a Future that never resolved, and
`keepalive()` was still running -- so the socket looked healthy from every
angle the code bothered to check.

Reconnect logic existed and did not help: it fires on an exception, and a
silent stall raises nothing. Google being overloaded is not NOVA's bug. Having
no way to notice is.

The watchdog is deliberately dumb: speech went up, nothing came back, and
after a while that is worth saying out loud. It is pure and clock-injected so
none of this waits on real time.
"""
from __future__ import annotations

import pytest

from desk.live_session import ResponseWatchdog


def _watchdog(timeout=20.0):
    w = ResponseWatchdog(timeout_seconds=timeout)
    w._now = lambda: 1000.0
    return w


def _at(w, when):
    w._now = lambda: when


def test_an_idle_session_is_not_stalled():
    """Nobody has said anything; silence is correct."""
    w = _watchdog()
    _at(w, 9999.0)
    assert w.stalled() is False


def test_a_prompt_answer_clears_the_clock():
    w = _watchdog()
    w.user_spoke()
    _at(w, 1002.0)
    w.model_responded()
    _at(w, 1100.0)
    assert w.stalled() is False


def test_speech_with_no_answer_becomes_a_stall():
    w = _watchdog(timeout=20.0)
    w.user_spoke()
    _at(w, 1010.0)
    assert w.stalled() is False, "twenty seconds is the limit, not ten"
    _at(w, 1021.0)
    assert w.stalled() is True


def test_further_speech_does_not_reset_the_clock():
    """The exact shape of the incident: four turns, none answered.

    Restarting the timer on each new turn would mean a user who keeps trying
    never triggers the watchdog -- the more they talk, the longer it stays
    broken.
    """
    w = _watchdog(timeout=20.0)
    w.user_spoke()
    for when in (1006.0, 1009.0, 1038.0):
        _at(w, when)
        w.user_spoke()
    _at(w, 1039.0)
    assert w.stalled() is True, "the clock was reset by the user trying again"


def test_a_stall_is_reported_once_not_every_check():
    w = _watchdog(timeout=20.0)
    w.user_spoke()
    _at(w, 1030.0)
    assert w.take_stall() is True
    assert w.take_stall() is False, "would reconnect on a loop"


def test_recovery_rearms_the_watchdog():
    w = _watchdog(timeout=20.0)
    w.user_spoke()
    _at(w, 1030.0)
    assert w.take_stall() is True

    w.model_responded()
    w.user_spoke()
    _at(w, 1032.0)
    assert w.stalled() is False
    _at(w, 1060.0)
    assert w.take_stall() is True, "a second stall went unnoticed"


def test_a_session_reset_forgets_everything():
    w = _watchdog(timeout=20.0)
    w.user_spoke()
    _at(w, 1030.0)
    w.reset()
    assert w.stalled() is False


def test_the_default_timeout_is_longer_than_a_slow_answer():
    """Turn 8 took 15.4 s to start playing. Cutting that off would be worse
    than the bug."""
    w = ResponseWatchdog()
    assert w.timeout_seconds >= 20.0, (
        f"{w.timeout_seconds}s would fire during a legitimately slow answer"
    )


def test_it_says_something_a_person_can_act_on():
    w = _watchdog(timeout=20.0)
    w.user_spoke()
    _at(w, 1030.0)
    message = w.message()
    assert "hear" in message.lower() or "reconnect" in message.lower(), message
    assert "Future" not in message and "Task" not in message, (
        f"that is a stack trace, not a sentence: {message}"
    )
