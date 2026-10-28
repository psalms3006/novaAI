"""A page NOVA read aloud cannot steer her next voice tool call.

Typed chat runs the rest of a turn untrusted once a tool returns outside
content (web, documents, mail). Voice never did: after web_search, an
open_app or computer_control call the page asked for ran with the person's
full authority. The voice session now carries what tainted the turn into
the next tool call, and clears it when the person speaks again.
"""
from __future__ import annotations

import asyncio
import sys
import types

from desk import live_session as ls


def _manager(monkeypatch, seen):
    fake = types.ModuleType("nova")

    def exec_sync(name, args, meta):
        from nova_core import trust
        seen.append((name, str(getattr(trust.current_trust(), "value", "")), trust.current_source()))
        return f"{name} ok"

    fake._execute_tool_sync = exec_sync
    monkeypatch.setitem(sys.modules, "nova", fake)
    monkeypatch.setattr(ls, "_load_meta", lambda: {})
    m = ls.LiveManager()
    m._publish = lambda ev: None
    return m


def _call(m, name, args):
    fc = types.SimpleNamespace(id=f"id-{name}", name=name, args=args)
    return asyncio.run(m._run_tool(fc))


def test_a_tool_after_web_search_runs_untrusted(monkeypatch):
    seen = []
    m = _manager(monkeypatch, seen)
    _call(m, "web_search", {"query": "free wallpapers"})
    _call(m, "open_app", {"app_name": "notepad"})
    assert seen[0][1] == "user"
    assert seen[1][0] == "open_app" and seen[1][1] == "untrusted"
    assert "web search" in seen[1][2]


def test_the_person_speaking_again_clears_it(monkeypatch):
    seen = []
    m = _manager(monkeypatch, seen)
    _call(m, "web_search", {"query": "x"})
    m._tainted_by = ""          # what the transcript handler does on new speech
    _call(m, "open_app", {"app_name": "notepad"})
    assert seen[-1][1] == "user"


def test_the_transcript_handler_resets_taint():
    import inspect
    src = inspect.getsource(ls.LiveManager)
    i = src.index("self._watchdog.user_spoke()")
    assert 'self._tainted_by = ""' in src[i:i + 200]
