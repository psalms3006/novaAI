"""Gemini's session time limit must end a connection gracefully, not break it.

From the real app (2026-09-26): every ~10 minutes Gemini sent GoAway and then
closed the socket itself -- "1008 ... the client failed to close the connection
after receiving a GoAway signal". NOVA never saw the GoAway: it has no
server_content, and the receive loop's `if sc is None: continue` skipped the
check at the bottom. One close landed mid-tool and a finished browser_control
result was lost ("send_tool_response failed"). And the session-resumption
handles Gemini sent were ignored, so every reconnect began a new conversation.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import time
import types

import pytest

from desk import live_session as ls


@pytest.fixture(scope="module")
def manager():
    return ls.LiveManager()


def _msg(**kw):
    base = dict(tool_call=None, tool_call_cancellation=None, server_content=None,
                session_resumption_update=None, go_away=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


class FakeSession:
    """receive() yields one batch of messages, then an empty turn (stream gone)."""

    def __init__(self, batches):
        self.batches = list(batches)

    async def receive(self):
        batch = self.batches.pop(0) if self.batches else []
        for m in batch:
            yield m


@pytest.mark.parametrize("value, seconds", [
    ("50s", 50.0), ("12.5s", 12.5), (30, 30.0), (dt.timedelta(seconds=7), 7.0), (None, None), ("soon", None),
])
def test_time_left_is_read_in_every_form_the_sdk_uses(value, seconds):
    assert ls._duration_s(value) == seconds


def test_the_receiver_keeps_the_handle_and_hears_go_away(manager):
    published = []
    manager._publish = published.append
    manager._resume_handle = None
    manager._go_away_deadline = None
    upd = lambda h, ok: types.SimpleNamespace(new_handle=h, resumable=ok)  # noqa: E731
    session = FakeSession([[
        _msg(session_resumption_update=upd("h1", True)),
        _msg(session_resumption_update=upd("h2", True)),
        _msg(session_resumption_update=upd("h3", False)),   # not resumable: keep h2
        _msg(go_away=types.SimpleNamespace(time_left="20s")),
    ]])
    with pytest.raises(ConnectionError):          # the stream then ends
        asyncio.run(manager._receiver(session))

    assert manager._resume_handle == "h2"
    assert manager._go_away_deadline is not None, "GoAway was skipped again"
    left = manager._go_away_deadline - time.monotonic()
    assert 10 < left <= 20 - ls.LiveManager.GO_AWAY_MARGIN_S
    assert any(e.type == "go_away" for e in published)


def _watch(manager, *, tools, deadline_in, run_for):
    calls = []
    manager._request_reconnect = lambda reason: calls.append(reason) or True
    manager._tool_tasks = set(tools)
    manager._go_away_deadline = time.monotonic() + deadline_in

    async def go():
        try:
            await asyncio.wait_for(manager._go_away_watch(), run_for)
        except asyncio.TimeoutError:
            pass
    asyncio.run(go())
    return calls


def test_it_waits_for_a_running_tool_before_reconnecting(manager):
    calls = _watch(manager, tools=[object()], deadline_in=10, run_for=0.8)
    assert calls == [], "reconnected while a tool was running; its result would be lost"


def test_it_reconnects_as_soon_as_it_is_quiet(manager):
    calls = _watch(manager, tools=[], deadline_in=10, run_for=2)
    assert calls and calls[0].startswith("GoAway")


def test_it_still_reconnects_before_gemini_does(manager):
    calls = _watch(manager, tools=[object()], deadline_in=-0.1, run_for=2)
    assert calls == ["GoAway deadline"]


def test_every_connection_resumes_the_last_session():
    src = inspect.getsource(ls)
    assert "SessionResumptionConfig(handle=self._resume_handle)" in src
    assert "tg.create_task(self._go_away_watch())" in src
