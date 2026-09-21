"""Giving up must not be permanent when the network comes back.

From the session that prompted this:

    01:22:13  receiver error: 1006 abnormal closure
    01:22:16  ... six reconnect attempts, all "timed out during opening handshake"
    01:24:19  giving up after 6 consecutive failures
    01:24:19  cancelled 1 pending task(s) on shutdown      <- mic dies with the session
    01:25:13  [CONNECTIVITY] state=online api_healthy=True <- 54 seconds later
    01:25:33 .. 01:31:41  online, healthy, every 20 seconds

NOVA exhausted its retry budget fifty-four seconds before the network
returned, then sat for six minutes logging that the network was healthy while
the microphone was gone. The connectivity monitor knew. Nothing was listening.

Two separate faults, and either alone reproduces it:

* a handshake timeout during an outage was classified as "the service refused
  us" rather than "there is no network", so it counted against a give-up
  budget that should not apply to a network outage
* giving up was permanent, with nothing to re-arm it

This covers both. The supervisor is the safety net: whatever the
classification, a healthy network and a dead session should not coexist.
"""
from __future__ import annotations

import pytest

from desk.live_session import LiveManager, VoiceSupervisor


class FakeLive:
    def __init__(self, state="error", gave_up=True):
        self._state = state
        self._gave_up = gave_up
        self.starts = 0

    def status(self):
        return {"state": self._state, "gave_up": self._gave_up}

    def start(self):
        self.starts += 1
        self._state = "connecting"
        self._gave_up = False
        return {"ok": True}


def _supervisor(live, healthy=True):
    return VoiceSupervisor(live_factory=lambda: live,
                           is_healthy=lambda: healthy)


def test_a_dead_session_is_restarted_when_the_network_is_healthy():
    live = FakeLive()
    _supervisor(live).check()
    assert live.starts == 1, "the session was left dead on a healthy network"


def test_it_does_not_restart_while_the_network_is_still_down():
    live = FakeLive()
    VoiceSupervisor(live_factory=lambda: live,
                    is_healthy=lambda: False).check()
    assert live.starts == 0, "retried into an outage"


def test_a_working_session_is_left_alone():
    live = FakeLive(state="connected", gave_up=False)
    _supervisor(live).check()
    assert live.starts == 0, "restarted a session that was working"


def test_a_session_still_trying_is_left_alone():
    live = FakeLive(state="connecting", gave_up=False)
    _supervisor(live).check()
    assert live.starts == 0


def test_it_does_not_restart_over_and_over():
    """A restart that fails must not become a hot loop."""
    live = FakeLive()
    supervisor = _supervisor(live)
    for _ in range(10):
        supervisor.check()
        live._state, live._gave_up = "error", True      # it failed again
    assert live.starts <= 3, f"restarted {live.starts} times in a row"


def test_a_later_recovery_is_still_attempted():
    """Backing off must not mean giving up a second time, permanently."""
    live = FakeLive()
    supervisor = _supervisor(live)
    supervisor._now = lambda: 1000.0
    supervisor.check()
    assert live.starts == 1

    live._state, live._gave_up = "error", True
    supervisor._now = lambda: 1000.0 + 3600
    supervisor.check()
    assert live.starts == 2, "a recovery an hour later was never attempted"


def test_a_broken_session_object_does_not_raise():
    class Broken:
        def status(self):
            raise RuntimeError("gone")

    VoiceSupervisor(live_factory=lambda: Broken(),
                    is_healthy=lambda: True).check()


def test_no_session_at_all_is_not_an_error():
    VoiceSupervisor(live_factory=lambda: None,
                    is_healthy=lambda: True).check()


# ── the classification half ─────────────────────────────────────────────────

def test_a_handshake_timeout_counts_as_a_possible_outage():
    """It was treated as a refusal, so it burned the give-up budget."""
    assert LiveManager._is_offline("timed out during opening handshake") is True


def test_name_resolution_failure_still_counts_as_offline():
    assert LiveManager._is_offline("getaddrinfo failed") is True


def test_a_real_refusal_is_not_mistaken_for_an_outage():
    assert LiveManager._is_offline("403 permission denied") is False
    assert LiveManager._is_offline("invalid api key") is False


# ── wiring ──────────────────────────────────────────────────────────────────

def test_the_supervisor_actually_runs():
    """Otherwise it is one more thing that exists and never executes."""
    import inspect

    import nova

    source = inspect.getsource(nova._start_ambient_intelligence)
    assert "VoiceSupervisor" in source, "nothing constructs the supervisor"
    assert "on_tick=" in source, (
        "the supervisor is built and never called, so a session that gave up "
        "stays dead exactly as before"
    )


def test_the_scheduler_runs_its_tick_hook(tmp_path):
    import time as _t

    from nova_scheduler import Scheduler, WorkflowRunner

    ticks = []
    scheduler = Scheduler(path=str(tmp_path / "w.json"))
    scheduler.start(WorkflowRunner(), interval_seconds=0.02,
                    on_tick=lambda: ticks.append(1))
    try:
        deadline = _t.time() + 5
        while _t.time() < deadline and not ticks:
            _t.sleep(0.01)
    finally:
        scheduler.stop()

    assert ticks, "the tick hook never ran"


def test_a_failing_tick_hook_does_not_stop_the_scheduler(tmp_path):
    import time as _t

    from nova_scheduler import Scheduler, WorkflowRunner

    calls = []

    def bad():
        calls.append(1)
        raise RuntimeError("supervisor blew up")

    scheduler = Scheduler(path=str(tmp_path / "w.json"))
    scheduler.start(WorkflowRunner(), interval_seconds=0.02, on_tick=bad)
    try:
        _t.sleep(0.3)
    finally:
        scheduler.stop()

    assert len(calls) > 1, "the loop died on the first failing tick"
