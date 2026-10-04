"""``python nova.py`` must be able to resume Gemini Live after an offline
excursion, not silently shut down.

nova.py's main() reuses the same TerminalVoice instance across both calls:

    status = asyncio.run(nova.run())          # returns "offline"
    ...
    run_offline_loop(meta)                     # blocks until network recovers
    status = asyncio.run(nova.run())           # meant to resume Gemini Live

TerminalVoice.run()'s ``finally`` block calls ``self._stop.set()`` on the way
out and nothing ever calls ``.clear()``. A ``threading.Event`` stays set once
set, so the second ``run()`` call enters ``_pump()`` with
``self._stop.is_set()`` already True -- the pump's ``while not
self._stop.is_set()`` loop body never runs, no event already sitting in the
subscriber queue is ever read, and ``_pump`` falls straight through to
``return "exit"``. nova.py treats that as a shutdown and the process ends,
right after the network came back.
"""
from __future__ import annotations

import asyncio
import queue

import desk.live_session as live_session
from terminal_voice import TerminalVoice


class FakeManager:
    def __init__(self):
        self.speaking = False
        self._q: queue.Queue = queue.Queue()
        self.started = 0
        self.stopped = 0

    def subscribe(self):
        return self._q

    def unsubscribe(self, q):
        pass

    def start(self):
        self.started += 1
        return {"ok": True}

    def stop(self):
        self.stopped += 1

    def send_text(self, text):
        return {"ok": True}

    def barge_in(self, notify_model=True):
        pass


def _push(mgr: FakeManager, kind: str, **data) -> None:
    mgr._q.put(live_session.LiveEvent(kind, **data))


def _run_once(tv: TerminalVoice) -> str:
    return asyncio.run(tv.run())


def test_run_actually_pumps_events_the_second_time_it_is_called():
    mgr = FakeManager()
    tv = TerminalVoice()
    tv._mgr = mgr
    tv._read_typed = lambda: None  # no real stdin in a test

    _push(mgr, "state", state="closed")
    assert _run_once(tv) == "exit"

    # Simulate nova.py's resume: the same TerminalVoice object, run() again.
    _push(mgr, "state", state="closed")
    _run_once(tv)

    assert mgr._q.qsize() == 0, (
        "the second run() never read the queued event -- _pump()'s loop "
        "exited before its first iteration because self._stop was never "
        "cleared, so a resumed session is indistinguishable from a closed "
        "one and nova.py shuts down instead of reconnecting"
    )


def test_run_restarts_the_session_on_each_call():
    mgr = FakeManager()
    tv = TerminalVoice()
    tv._mgr = mgr
    tv._read_typed = lambda: None

    _push(mgr, "state", state="closed")
    _run_once(tv)
    _push(mgr, "state", state="closed")
    _run_once(tv)

    assert mgr.started == 2


# ── the ready-timeout must not fire on a legitimate retry ──────────────────
#
# From nova.log, 2026-09-22: connect_start at 19:44:41.440, attempt 1 failed
# (plain 1006 abnormal closure) at 19:45:11.003 -- ~28s just for one attempt
# to fail -- backoff, then attempt 2 started at 19:45:13.017. _pump()'s
# READY_TIMEOUT_S=45s deadline was set once at connect_start and never
# reset, so it elapsed while attempt 2 was still legitimately in flight, and
# _pump() gave up and returned "offline" -- while [CONNECTIVITY] logged
# state=online continuously for the next several minutes. LiveManager
# publishes state="connecting" before every retry; _pump() read it and did
# nothing with it.

def test_a_legitimate_retry_does_not_burn_the_ready_timeout(monkeypatch):
    import terminal_voice as tvmod

    monkeypatch.setattr(tvmod, "READY_TIMEOUT_S", 0.3)
    mgr = FakeManager()
    tv = TerminalVoice()
    tv._mgr = mgr
    tv._read_typed = lambda: None

    async def scenario():
        task = asyncio.ensure_future(tv.run())
        # Attempt 1 "fails" partway through the original deadline...
        await asyncio.sleep(0.2)
        # ...and a retry starts. Without the fix this is read and ignored;
        # with it, the deadline is pushed out from here.
        _push(mgr, "state", state="connecting", retry_in_s=0.1)
        # Total elapsed since connect_start is about to cross the original
        # 0.3s deadline, but only ~0.2s has passed since the retry started.
        await asyncio.sleep(0.2)
        _push(mgr, "state", state="ready")
        await asyncio.sleep(0.05)
        tv._stop.set()
        return await task

    status = asyncio.run(scenario())
    assert status != "offline", (
        "a legitimate retry burned the ready-timeout budget instead of "
        "resetting it, so a session that was still actively connecting "
        "was declared offline"
    )


def test_a_connection_with_no_sign_of_life_still_gives_up(monkeypatch):
    """The timeout is a real safety net for a connection that is truly
    stuck -- silence with no "connecting" events at all must still time
    out, or a genuinely dead session would hang forever."""
    import terminal_voice as tvmod

    monkeypatch.setattr(tvmod, "READY_TIMEOUT_S", 0.15)
    mgr = FakeManager()
    tv = TerminalVoice()
    tv._mgr = mgr
    tv._read_typed = lambda: None

    assert _run_once(tv) == "offline"
