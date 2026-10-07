"""Held messages must eventually be said, and said as words.

Before: `ProactiveAgent.flush()` had no caller anywhere, so anything queued
because NOVA was mid-sentence, or a MEDIUM finding inside the spacing gap,
was never delivered. And the launch greeting read `pending()` -- a count --
into the opening line, so NOVA would have said "2".
"""
from __future__ import annotations

import time

from nova_proactive import ProactiveAgent, ProactiveEvent, Priority


def _agent():
    said = []
    a = ProactiveAgent(speak_fn=said.append)
    clock = {"t": 1000.0}
    a._now = lambda: clock["t"]
    return a, said, clock


def test_a_message_held_while_speaking_is_said_once_she_stops():
    a, said, _ = _agent()
    busy = {"v": True}
    a.set_busy(lambda: busy["v"])
    a.emit(ProactiveEvent("completion", "The report is ready.", Priority.HIGH))
    assert said == [] and a.pending() == 1
    assert a.tick() is False                      # still talking
    busy["v"] = False
    assert a.tick() is True
    assert said == ["The report is ready."] and a.pending() == 0


def test_tick_keeps_the_spacing_rule_for_ordinary_findings():
    a, said, clock = _agent()
    a.emit(ProactiveEvent("discovery", "first", Priority.MEDIUM))
    a.emit(ProactiveEvent("discovery", "second", Priority.MEDIUM))
    assert said == ["first"]
    assert a.tick() is False                      # gap not elapsed
    clock["t"] += a.MIN_GAP_S
    assert a.tick() is True and said == ["first", "second"]


def test_the_background_ticker_actually_delivers():
    a, said, _ = _agent()
    a.TICK_S = 0.05
    busy = {"v": True}
    a.set_busy(lambda: busy["v"])
    a.start()
    try:
        a.emit(ProactiveEvent("completion", "Done.", Priority.HIGH))
        busy["v"] = False
        deadline = time.time() + 2
        while not said and time.time() < deadline:
            time.sleep(0.02)
        assert said == ["Done."]
    finally:
        a.stop()


def test_the_opening_line_contains_the_messages_not_a_count(monkeypatch):
    import nova_core_voice
    a, _, _ = _agent()
    a.set_busy(lambda: True)
    a.emit(ProactiveEvent("completion", "Your build finished.", Priority.HIGH))
    a.emit(ProactiveEvent("error", "The backup failed.", Priority.HIGH))
    import types, sys
    fake = types.ModuleType("nova")
    fake._proactive = a
    monkeypatch.setitem(sys.modules, "nova", fake)
    line = nova_core_voice.opening_line()
    assert "Your build finished." in line and "The backup failed." in line
    assert line.strip() != "2"
