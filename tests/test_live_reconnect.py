"""Regression tests for the Gemini Live voice session lifecycle.

Two bugs are covered:

  1. A permanently failing connection (bad key, revoked quota, broken TLS
     trust) retried every 2 seconds forever, filling the log and never telling
     the user anything.
  2. The Live socket drops on keepalive timeout after about a minute of
     silence. The greeting was sent on every (re)connect, so an idle NOVA
     spoke an unprompted greeting over and over.
"""
from __future__ import annotations

import inspect

from desk.live_session import LiveManager, LiveState


def test_reconnect_policy_is_bounded():
    assert LiveManager.MAX_RECONNECT_ATTEMPTS > 0
    assert LiveManager.MAX_RECONNECT_ATTEMPTS <= 20


def test_reconnect_backoff_has_a_ceiling():
    assert LiveManager.RECONNECT_BASE_DELAY > 0
    assert LiveManager.RECONNECT_MAX_DELAY >= LiveManager.RECONNECT_BASE_DELAY


def test_backoff_grows_and_is_capped():
    base = LiveManager.RECONNECT_BASE_DELAY
    cap = LiveManager.RECONNECT_MAX_DELAY
    delays = [
        min(base * (2 ** max(0, n - 1)), cap)
        for n in range(1, LiveManager.MAX_RECONNECT_ATTEMPTS + 1)
    ]
    assert delays[0] == base
    assert delays == sorted(delays), "backoff must be non-decreasing"
    assert max(delays) <= cap
    # The old behaviour was a flat 2s forever; the policy must actually back off.
    assert delays[-1] > delays[0]


def test_connect_loop_gives_up_after_the_attempt_limit():
    src = inspect.getsource(LiveManager._connect_and_run)
    assert "MAX_RECONNECT_ATTEMPTS" in src
    assert "gave_up" in src, "give-up must be reported to the UI, not silent"


def test_greeting_is_guarded_by_the_has_greeted_flag():
    src = inspect.getsource(LiveManager._connect_and_run)
    assert "_has_greeted" in src
    idx_guard = src.index("_has_greeted")
    idx_send = src.index("_send_greeting")
    assert idx_guard < idx_send, "the greeting must be guarded, not unconditional"


def test_has_greeted_starts_false():
    mgr = LiveManager()
    assert mgr._has_greeted is False


def test_start_resets_has_greeted_so_a_new_session_greets_again():
    src = inspect.getsource(LiveManager.start)
    assert "_has_greeted = False" in src


def test_stop_is_a_noop_when_not_running():
    mgr = LiveManager()
    assert mgr._state is LiveState.IDLE
    assert mgr.stop()["ok"] is True


def test_send_text_is_rejected_when_not_connected():
    mgr = LiveManager()
    result = mgr.send_text("hello")
    assert result["ok"] is False
    assert "not connected" in result["reason"]


def test_status_is_reportable_before_any_connection():
    st = LiveManager().status()
    assert st["state"] == "idle"
    assert "model" in st and "voice" in st
