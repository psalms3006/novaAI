"""OllamaProvider.stream_complete() -- without it, IntelligenceRouter.
stream_complete() silently fell back to this provider's plain complete(),
so offline voice waited for Ollama's entire reply before speaking a
single sentence of it, regardless of how long the reply was.
"""
from __future__ import annotations

import json

from nova_intelligence.ollama_provider import OllamaProvider


class _FakeResponse:
    """Stands in for requests.post(..., stream=True)'s return value."""

    def __init__(self, lines: list[dict]):
        self._lines = [json.dumps(l).encode() for l in lines]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_lines(self):
        return iter(self._lines)


def _provider_with_fake_stream(monkeypatch, lines):
    provider = OllamaProvider(model="qwen2.5:3b")
    monkeypatch.setattr(
        "nova_intelligence.ollama_provider.requests.post",
        lambda *a, **kw: _FakeResponse(lines),
    )
    return provider


def test_text_chunks_are_yielded_as_they_arrive(monkeypatch):
    lines = [
        {"message": {"content": "Hello"}, "done": False},
        {"message": {"content": " there"}, "done": False},
        {"message": {"content": "."}, "done": True},
    ]
    provider = _provider_with_fake_stream(monkeypatch, lines)

    events = list(provider.stream_complete([{"role": "user", "content": "hi"}]))

    text_events = [payload for kind, payload in events if kind == "text"]
    assert text_events == ["Hello", " there", "."]


def test_the_final_event_carries_the_full_accumulated_text(monkeypatch):
    lines = [
        {"message": {"content": "One "}, "done": False},
        {"message": {"content": "two "}, "done": False},
        {"message": {"content": "three."}, "done": True},
    ]
    provider = _provider_with_fake_stream(monkeypatch, lines)

    events = list(provider.stream_complete([{"role": "user", "content": "count"}]))

    kind, result = events[-1]
    assert kind == "done"
    assert result.text == "One two three."
    assert result.error == ""


def test_tool_calls_in_the_final_chunk_are_parsed():
    provider = OllamaProvider(model="qwen2.5:3b")
    lines = [
        {"message": {"content": ""}, "done": False},
        {
            "message": {
                "content": "",
                "tool_calls": [
                    {"function": {"name": "open_app",
                                  "arguments": {"app_name": "Notepad"}}}
                ],
            },
            "done": True,
        },
    ]
    import nova_intelligence.ollama_provider as mod
    orig = mod.requests.post
    mod.requests.post = lambda *a, **kw: _FakeResponse(lines)
    try:
        events = list(provider.stream_complete([{"role": "user", "content": "open notepad"}]))
    finally:
        mod.requests.post = orig

    kind, result = events[-1]
    assert kind == "done"
    assert result.tool_calls == [{"name": "open_app", "args": {"app_name": "Notepad"}}]


def test_a_request_failure_raises_rather_than_yielding_an_error_string(monkeypatch):
    """The router can only fail over to another provider if the failure
    is visible as an exception -- a yielded error string looks like an
    answer, exactly as the existing stream() method's own comment says."""
    provider = OllamaProvider(model="qwen2.5:3b")

    def _raise(*a, **kw):
        raise ConnectionError("ollama not running")

    monkeypatch.setattr("nova_intelligence.ollama_provider.requests.post", _raise)

    import pytest
    with pytest.raises(ConnectionError):
        list(provider.stream_complete([{"role": "user", "content": "hi"}]))
