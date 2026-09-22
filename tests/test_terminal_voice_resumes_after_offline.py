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
