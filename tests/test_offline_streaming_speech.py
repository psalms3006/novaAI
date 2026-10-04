"""Offline mode's TTS used to wait for Ollama's entire reply before
speaking a word of it (the router was called with the ordinary,
non-streaming complete(), regardless of reply length). These pin the
streaming replacement: sentences are spoken as they arrive, and nothing
gets said twice.
"""
from __future__ import annotations

import threading

import pytest

import nova
import offline_extra


class _FakeGenerateResult:
    def __init__(self, text="", tool_calls=None, error=""):
        self.text = text
        self.tool_calls = tool_calls or []
        self.error = error

    @property
    def ok(self):
        return not self.error and (self.text or self.tool_calls)


class _FakeRouter:
    """Replays a fixed sequence of ("text", chunk) / ("done", result)
    events, the same shape IntelligenceRouter.stream_complete yields.

    Also answers complete() (the plain, non-streaming call) with the same
    "done" result, concatenated text included -- _get_offline_response_v2
    calls router.complete() directly as its own first attempt, and without
    this a test whose streaming path intentionally returns None (to
    exercise the fallback) would fall through past the fake router
    entirely and hit this test process's real network/Ollama absence.
    """

    def __init__(self, events):
        self._events = events
        self.calls = []

    def stream_complete(self, **kwargs):
        self.calls.append(kwargs)
        for ev in self._events:
            yield ev

    def complete(self, **kwargs):
        text = "".join(p for k, p in self._events if k == "text")
        for kind, payload in self._events:
            if kind == "done":
                if text and not payload.text:
                    payload.text = text
                return payload
        return _FakeGenerateResult(text=text)


def _install_router(monkeypatch, events):
    router = _FakeRouter(events)
    monkeypatch.setattr(nova, "_nova_router", router, raising=False)
    return router


# ── _get_offline_response_streaming ─────────────────────────────────────────

def test_complete_sentences_are_spoken_as_they_stream(monkeypatch):
    _install_router(monkeypatch, [
        ("text", "Hello there. "),
        ("text", "How are you? "),
        ("text", "I am fine."),
        ("done", _FakeGenerateResult()),
    ])
    spoken = []
    result = offline_extra._get_offline_response_streaming(
        [{"role": "user", "content": "hi"}], use_tools=False,
        speak_fn=lambda text, block=False: spoken.append(text),
    )

    assert spoken == ["Hello there.", "How are you?", "I am fine."]
    assert result["already_spoken"] is True
    assert result["text"] == "Hello there. How are you? I am fine."


def test_a_trailing_sentence_with_no_final_punctuation_is_still_spoken(monkeypatch):
    _install_router(monkeypatch, [
        ("text", "This has no ending"),
        ("done", _FakeGenerateResult()),
    ])
    spoken = []
    offline_extra._get_offline_response_streaming(
        [{"role": "user", "content": "hi"}], use_tools=False,
        speak_fn=lambda text, block=False: spoken.append(text),
    )
    assert spoken == ["This has no ending"]


def test_tool_calls_are_carried_through(monkeypatch):
    _install_router(monkeypatch, [
        ("text", "Let me check. "),
        ("done", _FakeGenerateResult(
            tool_calls=[{"name": "web_search", "args": {"query": "x"}}])),
    ])
    spoken = []
    result = offline_extra._get_offline_response_streaming(
        [{"role": "user", "content": "hi"}], use_tools=True,
        speak_fn=lambda text, block=False: spoken.append(text),
    )
    assert spoken == ["Let me check."]
    assert result["tool_calls"] == [{"name": "web_search", "args": {"query": "x"}}]


def test_returns_none_when_no_router_is_registered(monkeypatch):
    monkeypatch.setattr(nova, "_nova_router", None, raising=False)
    result = offline_extra._get_offline_response_streaming(
        [{"role": "user", "content": "hi"}], use_tools=False,
        speak_fn=lambda text, block=False: None,
    )
    assert result is None


def test_returns_none_on_a_failed_result_with_nothing_spoken(monkeypatch):
    _install_router(monkeypatch, [
        ("done", _FakeGenerateResult(error="model unavailable")),
    ])
    spoken = []
    result = offline_extra._get_offline_response_streaming(
        [{"role": "user", "content": "hi"}], use_tools=False,
        speak_fn=lambda text, block=False: spoken.append(text),
    )
    assert result is None
    assert spoken == []


# ── think_offline_v2: no double-speaking ────────────────────────────────────

@pytest.fixture
def clean_history(monkeypatch):
    monkeypatch.setattr(nova, "conversation_history", [], raising=False)
    monkeypatch.setattr(offline_extra, "build_memory_context", lambda meta, query="": "")
    monkeypatch.setattr(offline_extra, "agent_process", lambda msg, meta: None)
    monkeypatch.setattr(offline_extra, "_trim_history", lambda: None)


def test_think_offline_v2_with_speak_fn_speaks_exactly_once(monkeypatch, clean_history):
    _install_router(monkeypatch, [
        ("text", "A short reply."),
        ("done", _FakeGenerateResult()),
    ])
    spoken = []
    offline_extra.think_offline_v2(
        "hello", {}, speak_fn=lambda text, block=False: spoken.append(text))

    assert spoken == ["A short reply."]  # not spoken again as a full-text block


def test_think_offline_v2_without_speak_fn_does_not_speak(monkeypatch, clean_history):
    """Every existing text-only caller (desk/chat.py, server_extra.py)
    calls this with no speak_fn and must see unchanged behaviour."""
    _install_router(monkeypatch, [
        ("text", "A short reply."),
        ("done", _FakeGenerateResult()),
    ])
    text = offline_extra.think_offline_v2("hello", {})
    assert text == "A short reply."


def test_an_empty_streamed_reply_still_speaks_a_fallback_text(monkeypatch, clean_history):
    """An empty result (no text, no tool_calls -- GenerateResult.ok is
    False) is not "already spoken" content, so it must never be silently
    swallowed. In this test it cascades all the way to "all brain engines
    offline" -- the fake router's result isn't .ok, so
    _get_offline_response_v2 doesn't accept it either and falls through
    its own legacy cascade -- but any of the fallback strings this can
    produce must actually be spoken, not just returned as text."""
    _install_router(monkeypatch, [
        ("done", _FakeGenerateResult(text="")),
    ])
    # Deterministic: skip the legacy cascade's real network/Ollama probes
    # entirely rather than depending on this machine's actual connectivity.
    monkeypatch.setattr(offline_extra, "is_online", lambda: False)
    monkeypatch.setattr(offline_extra, "is_ollama_running", lambda: False)

    spoken = []
    offline_extra.think_offline_v2(
        "hello", {}, speak_fn=lambda text, block=False: spoken.append(text))

    assert spoken, "no fallback text was ever spoken"
    assert spoken[-1] in (
        "Done.", "I couldn't generate a response.",
        "All brain engines offline. Check your connection or start Ollama.",
    )
