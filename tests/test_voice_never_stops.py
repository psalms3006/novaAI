"""Nothing but the user may silence NOVA's voice.

Observed 2026-09-24 07:42-07:45 (nova.log): the model stopped answering, the
stall watchdog fired after 25 s and logged "the connection looks stuck, so
I'm reconnecting" -- and then did not reconnect for two and a half minutes.
Its only lever was posting ``None`` to the mic queue, which ends the *mic
sender* and nothing else: the session is a TaskGroup, a sibling that returns
normally cancels no one, so the receiver went on waiting on the dead
connection. With nothing draining the mic queue, 377 frames of the user's
speech were dropped ("mic queue full ... the sender is not keeping up") until
Google itself aborted the socket (1008).

Separately, tool calls were awaited inside the receiver loop, so for as long
as any tool ran (web_search 9.3 s, a confirmation 7.8 s) NOVA read nothing
from the connection at all; and the watchdog, which only counted model
*audio* as a response, would call a 25 s research tool a stall and tear the
connection down underneath it.
"""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from desk import live_session as ls


# ── the watchdog understands that a tool call is an answer ──────────────────

def _watchdog(timeout=20.0):
    w = ls.ResponseWatchdog(timeout_seconds=timeout)
    w._now = lambda: 1000.0
    return w


def _at(w, when):
    w._now = lambda: when


def test_a_long_running_tool_is_not_a_stall():
    w = _watchdog(timeout=20.0)
    w.user_spoke()
    _at(w, 1001.0)
    w.tool_started()
    _at(w, 1200.0)                 # a 199 s research tool
    assert w.stalled() is False
    assert w.take_stall() is False


def test_the_clock_restarts_when_the_tool_result_goes_back():
    w = _watchdog(timeout=20.0)
    w.user_spoke()
    w.tool_started()
    _at(w, 1100.0)
    w.tool_finished()
    _at(w, 1115.0)
    assert w.stalled() is False
    _at(w, 1121.0)                 # result sent, nothing back for 21 s
    assert w.stalled() is True


def test_a_finished_turn_counts_as_an_answer():
    w = _watchdog(timeout=20.0)
    w.user_spoke()
    w.model_responded()            # turn_complete with no audio
    _at(w, 1100.0)
    assert w.stalled() is False


# ── a stall actually reconnects ─────────────────────────────────────────────

class StuckSession:
    """Connected, accepting audio, never answering -- the 07:42 session."""

    async def send_realtime_input(self, **kw):
        pass

    async def send_client_content(self, **kw):
        pass

    async def send_tool_response(self, **kw):
        pass

    def receive(self):
        async def gen():
            await asyncio.Event().wait()       # never yields, never ends
            yield None
        return gen()


class FakeLive:
    def __init__(self, connects):
        self.connects = connects

    def connect(self, model, config):
        connects = self.connects

        class _Ctx:
            async def __aenter__(self):
                s = StuckSession()
                connects.append(s)
                return s

            async def __aexit__(self, *exc):
                return False
        return _Ctx()


@pytest.fixture
def isolated_manager(monkeypatch):
    connects = []
    published = []
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(ls, "genai", SimpleNamespace(
        Client=lambda api_key=None: SimpleNamespace(
            aio=SimpleNamespace(live=FakeLive(connects)))))
    monkeypatch.setattr(ls, "_relax_websocket_keepalive", lambda c: None)
    monkeypatch.setattr(ls, "_load_meta", lambda: {})
    monkeypatch.setattr(ls, "_build_memory_context", lambda meta: "")
    monkeypatch.setattr(ls, "_identity_block", lambda: "")
    m = ls.LiveManager()
    m._has_greeted = True
    m._start_playback = lambda: True
    m._start_mic = lambda: None
    m._stop_mic = lambda: None
    m._stop_playback = lambda: None
    m._publish = lambda ev: published.append(ev)
    m.RECONNECT_BASE_DELAY = 0.05
    m._state = ls.LiveState.CONNECTING

    def run():
        m._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(m._loop)
        try:
            m._loop.run_until_complete(m._connect_and_run())
        except Exception:
            pass
    t = threading.Thread(target=run, daemon=True)
    t.start()
    yield m, connects, published
    with m._state_lock:
        m._state = ls.LiveState.CLOSED
    m._request_reconnect("test over")
    t.join(timeout=5)


def _wait(cond, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_a_stall_tears_the_stuck_connection_down_and_reconnects(isolated_manager):
    m, connects, published = isolated_manager
    assert _wait(lambda: len(connects) == 1), "never connected"
    m._on_stall()                          # what the mic callback calls
    assert _wait(lambda: len(connects) == 2), (
        "the stall was announced but the stuck connection was never "
        "replaced -- NOVA stays mute until Google gives up on the socket")


def test_a_requested_reconnect_is_not_reported_as_a_failure(isolated_manager):
    m, connects, published = isolated_manager
    assert _wait(lambda: len(connects) == 1)
    for _ in range(ls.LiveManager.MAX_RECONNECT_ATTEMPTS + 1):
        n = len(connects)
        m._on_stall()
        m._watchdog.reset()
        assert _wait(lambda: len(connects) == n + 1)
    states = [e.data.get("state") for e in published if e.type == "state"]
    assert "error" not in states, "a deliberate reconnect turned the orb red"


# ── a tool runs beside the conversation, not in front of it ─────────────────

def _bare_manager():
    m = ls.LiveManager.__new__(ls.LiveManager)
    m._subscribers, m._subs_lock = [], threading.Lock()
    m._publish = lambda ev: None
    m._watchdog = ls.ResponseWatchdog()
    m._tool_tasks = set()
    m._cancelled_tool_ids = set()
    return m


class RecordingSession:
    def __init__(self):
        self.sent = []

    async def send_tool_response(self, function_responses):
        self.sent.append(function_responses)


def _call(call_id="c1"):
    return SimpleNamespace(function_calls=[
        SimpleNamespace(name="web_search", id=call_id, args={})])


def test_the_receiver_keeps_reading_while_a_tool_runs():
    m = _bare_manager()
    release = threading.Event()

    async def slow_tool(fc):
        await asyncio.to_thread(release.wait, 5)
        return {"id": fc.id, "name": fc.name, "response": {"result": "ok"}}
    m._run_tool = slow_tool
    s = RecordingSession()

    async def scenario():
        t0 = time.monotonic()
        m._spawn_tool_calls(s, _call())
        assert time.monotonic() - t0 < 0.1, "the receiver waited for the tool"
        await asyncio.sleep(0.05)
        assert m._watchdog.stalled() is False
        release.set()
        await asyncio.wait_for(asyncio.gather(*m._tool_tasks), 5)

    asyncio.run(scenario())
    assert s.sent and s.sent[0][0]["id"] == "c1", "the result never went back"


def test_a_cancelled_tool_call_is_not_answered():
    """The user talked over it and Gemini dropped the call. Answering anyway
    replays a stale result into a conversation that has moved on."""
    m = _bare_manager()

    async def tool(fc):
        m._cancel_tool_calls(["c1"])           # cancellation lands mid-run
        return {"id": fc.id, "name": fc.name, "response": {"result": "ok"}}
    m._run_tool = tool
    s = RecordingSession()

    async def scenario():
        m._spawn_tool_calls(s, _call())
        await asyncio.wait_for(asyncio.gather(*m._tool_tasks), 5)

    asyncio.run(scenario())
    assert s.sent == []


# ── slow work runs while she keeps talking ──────────────────────────────────
# Measured against gemini-3.1-flash-live-preview on 2026-09-24: with a
# NON_BLOCKING declaration the model spoke, called the tool, and spoke the
# result once it arrived (scheduling WHEN_IDLE) without cutting itself off.

def test_slow_work_tools_are_declared_non_blocking_to_the_live_model():
    decls = ls._live_tool_declarations([
        {"name": "web_search", "description": "d", "parameters": {}},
        {"name": "open_app", "description": "d", "parameters": {}},
        {"name": "computer_settings", "description": "d", "parameters": {}},
    ])
    by = {d["name"]: d for d in decls}
    assert by["web_search"].get("behavior") == "NON_BLOCKING"
    # Quick actions, and anything that needs the user's agreement, still wait
    # for their outcome -- she must not claim a result she has not got.
    assert "behavior" not in by["open_app"]
    assert "behavior" not in by["computer_settings"]


def test_the_shared_declarations_are_not_modified():
    import copy
    original = [{"name": "web_search", "description": "d", "parameters": {}}]
    before = copy.deepcopy(original)
    ls._live_tool_declarations(original)
    assert original == before, "the text chat's tool list was changed too"


def test_a_non_blocking_result_waits_for_her_to_finish_speaking():
    m = _bare_manager()
    m._execute_tool = lambda name, args: "found it"

    async def run(name):
        return await m._run_tool(SimpleNamespace(name=name, id="c", args={}))

    slow = asyncio.run(run("web_search"))
    quick = asyncio.run(run("open_app"))
    assert str(slow.scheduling).endswith("WHEN_IDLE")
    assert quick.scheduling is None


# ── talking over her works, and a busy CPU cannot take the mic down ─────────
# Barge-in had been switched off entirely: the gate ran half-duplex (mic muted
# while NOVA speaks, no voice interruption) because the echo-cancelling
# policy ran inside the PortAudio callback and a loaded machine dropped mic
# blocks. Measured 2026-09-24 on this machine the full gate costs 0.03 ms per
# 64 ms frame (median), 8.7 ms at worst -- the budget problem was running it
# on the real-time thread at all, under 100% CPU.

def test_the_desktop_voice_can_be_interrupted_by_voice_by_default(monkeypatch):
    monkeypatch.delenv("NOVA_VOICE_FULL_DUPLEX", raising=False)
    monkeypatch.setattr(ls, "_interrupt_preference", lambda: True)
    assert ls._voice_barge_in_enabled() is True
    assert ls.LiveManager()._gate.simple is False


def test_barge_in_can_still_be_turned_off(monkeypatch):
    monkeypatch.delenv("NOVA_VOICE_FULL_DUPLEX", raising=False)
    monkeypatch.setattr(ls, "_interrupt_preference", lambda: False)
    assert ls._voice_barge_in_enabled() is False
    monkeypatch.setattr(ls, "_interrupt_preference", lambda: True)
    monkeypatch.setenv("NOVA_VOICE_FULL_DUPLEX", "0")
    assert ls._voice_barge_in_enabled() is False


class FakeInputStream:
    last = None

    def __init__(self, callback=None, **kw):
        self.callback = callback
        FakeInputStream.last = self

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass


def test_the_audio_callback_does_no_processing(monkeypatch):
    monkeypatch.setattr(ls, "HAS_SD", True)
    monkeypatch.setattr(ls, "sd", SimpleNamespace(InputStream=FakeInputStream), raising=False)
    monkeypatch.setattr(ls, "_mic_device_resolved", lambda: None)
    monkeypatch.setattr(ls, "_mic_device", lambda: None)
    m = ls.LiveManager()
    seen = []

    def slow_process(frame):
        seen.append(threading.current_thread().name)
        time.sleep(0.2)                 # a CPU-starved moment
        return frame.tobytes()
    m._gate.process = slow_process

    loop = asyncio.new_event_loop()
    try:
        m._loop = loop
        m._mic_queue = asyncio.Queue(maxsize=ls.MIC_QUEUE_FRAMES)
        m._start_mic()
        frame = np.zeros((ls.MIC_BLOCK, 1), dtype=np.int16)
        t0 = time.monotonic()
        FakeInputStream.last.callback(frame, ls.MIC_BLOCK, None, None)
        assert time.monotonic() - t0 < 0.05, "the real-time callback did the work"
        assert _wait(lambda: bool(seen), timeout=3)
        assert seen[0] != threading.current_thread().name
        assert "mic" in seen[0]
    finally:
        m._stop_mic()
        loop.close()
