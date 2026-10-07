"""A connection that opens and is closed at once is not "reconnected".

The failure counter used to reset the moment a socket opened, so a session
the server closed straight after setup (a dead resume handle, a policy
reject) retried every couple of seconds forever and the window showed
"Reconnecting" indefinitely. It now counts until the connection has stayed
up for STABLE_CONNECTION_S, gives up visibly after MAX_RECONNECT_ATTEMPTS,
and drops a resume handle that keeps failing.
"""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

from desk import live_session as ls


class DyingSession:
    async def send_realtime_input(self, **kw):
        pass

    async def send_client_content(self, **kw):
        pass

    async def send_tool_response(self, **kw):
        pass

    def receive(self):
        async def gen():
            await asyncio.sleep(0.01)
            raise RuntimeError("1008 policy violation: session closed by server")
            yield None
        return gen()


def _manager(monkeypatch, handles):
    connects, published = [], []

    class Live:
        def connect(self, model, config):
            handles.append(getattr(config.session_resumption, "handle", None))

            class _Ctx:
                async def __aenter__(self_inner):
                    s = DyingSession()
                    connects.append(s)
                    return s

                async def __aexit__(self_inner, *exc):
                    return False
            return _Ctx()

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(ls, "genai", SimpleNamespace(
        Client=lambda api_key=None: SimpleNamespace(aio=SimpleNamespace(live=Live()))))
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
    m.RECONNECT_BASE_DELAY = 0.01
    m.RECONNECT_MAX_DELAY = 0.02
    m._state = ls.LiveState.CONNECTING
    m._resume_handle = "stale-handle-from-yesterday"
    return m, connects, published


def _run(m, timeout=10.0):
    def go():
        m._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(m._loop)
        try:
            m._loop.run_until_complete(m._connect_and_run())
        except Exception:
            pass
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout)
    return not t.is_alive()


def test_connect_then_die_gives_up_instead_of_looping_forever(monkeypatch):
    handles = []
    m, connects, published = _manager(monkeypatch, handles)
    finished = _run(m)
    if not finished:
        with m._state_lock:
            m._state = ls.LiveState.CLOSED
    assert finished, f"still reconnecting after {len(connects)} connections"
    assert len(connects) == m.MAX_RECONNECT_ATTEMPTS
    gave_up = [e for e in published if e.type == "state" and e.data.get("gave_up")]
    assert gave_up, "the user was never told voice had stopped"


def test_a_resume_handle_that_keeps_failing_is_dropped(monkeypatch):
    handles = []
    m, connects, published = _manager(monkeypatch, handles)
    _run(m)
    assert handles[0] == "stale-handle-from-yesterday"
    assert handles[-1] is None, "every retry replayed the dead handle"


def test_offline_token_mint_is_an_outage_not_a_refusal():
    assert ls.LiveManager._is_offline(
        "RuntimeError: NOVA Cloud is unreachable. Check your connection.")
    assert ls.LiveManager._is_offline(
        "HTTPSConnectionPool(host='api.omniel.com.ng'): Max retries exceeded "
        "(Caused by NewConnectionError: Failed to establish a new connection)")


def test_the_supervisor_does_not_restart_a_rejected_key():
    class M:
        started = 0

        def status(self):
            return {"state": "error", "gave_up": True, "auth_rejected": True}

        def start(self):
            M.started += 1

    sup = ls.VoiceSupervisor(live_factory=lambda: M(), is_healthy=lambda: True)
    for _ in range(5):
        sup.check()
    assert M.started == 0
