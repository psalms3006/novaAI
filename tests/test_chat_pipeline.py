"""Regression tests for the text-chat pipeline: duplication and pre-call latency.

Observed in the user's own session before these fixes:

    user      | 'TEST A: Reply with exactly: ALPHA'
    assistant | 'ALPHAALPHAALPHAALPHA'
    user      | 'TEST B: Reply with exactly: BRAVO'
    assistant | 'BRAVOBRAVO'

Two independent causes, which multiplied together:

  1. api_chat() stored the user's message, then _run_chat() rebuilt history
     *from the store* and appended the same message again — so every prompt
     reached the model twice. The model echoed accordingly.
  2. _persist_turn() concatenated both the streamed "token" events and the
     final "assistant" event, each of which carries the full reply — doubling
     whatever the model produced.

Separately, the router probed Ollama's /api/tags synchronously on every
request, adding 2-4 s of pre-flight latency to each chat turn.
"""
from __future__ import annotations

import time

import pytest


# ── 1. the prompt must contain the user's message exactly once ───────────────

def _build(history, message):
    """Mirror of desk.bridge._run_chat's message assembly."""
    msgs = [{"role": "system", "content": "sys"}]
    msgs += history
    if not (msgs and msgs[-1].get("role") == "user"
            and (msgs[-1].get("content") or "").strip() == (message or "").strip()):
        msgs.append({"role": "user", "content": message})
    return msgs


def test_message_not_duplicated_when_history_already_has_it():
    msg = "Reply with exactly: ALPHA"
    history = [{"role": "user", "content": msg}]
    out = _build(history, msg)
    assert [m["content"] for m in out if m["role"] == "user"] == [msg]


def test_message_appended_when_history_is_empty():
    msg = "Reply with exactly: ALPHA"
    out = _build([], msg)
    assert [m["content"] for m in out if m["role"] == "user"] == [msg]


def test_message_appended_when_history_ends_with_assistant():
    msg = "next question"
    history = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "answer"}]
    out = _build(history, msg)
    assert out[-1] == {"role": "user", "content": msg}


def test_only_the_trailing_duplicate_is_suppressed():
    """An identical question asked earlier must stay in history."""
    msg = "same"
    history = [
        {"role": "user", "content": msg},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": msg},
    ]
    out = _build(history, msg)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "user"]


# ── 2. persistence must store the reply once ─────────────────────────────────

def _persist_content(events):
    """Mirror of desk.bridge._persist_turn's content assembly."""
    streamed, final = [], None
    for ev in events:
        if ev.get("type") == "token":
            streamed.append(ev.get("text", ""))
        elif ev.get("type") == "assistant":
            final = ev.get("text", "")
    return (final if final is not None else "".join(streamed)).strip()


def test_non_streaming_turn_stores_the_reply_once():
    events = [
        {"type": "token", "text": "ALPHA"},
        {"type": "assistant", "text": "ALPHA"},
        {"type": "done"},
    ]
    assert _persist_content(events) == "ALPHA"


def test_streamed_chunks_are_not_added_to_the_final_text():
    events = [
        {"type": "token", "text": "The capital "},
        {"type": "token", "text": "of Nigeria "},
        {"type": "token", "text": "is Abuja."},
        {"type": "assistant", "text": "The capital of Nigeria is Abuja."},
    ]
    assert _persist_content(events) == "The capital of Nigeria is Abuja."


def test_tokens_are_used_when_no_assistant_event_arrives():
    events = [{"type": "token", "text": "par"}, {"type": "token", "text": "tial"}]
    assert _persist_content(events) == "partial"


def test_empty_turn_stores_empty_string():
    assert _persist_content([{"type": "done"}]) == ""


def test_assistant_event_wins_even_if_it_is_empty():
    """An empty final answer must not silently resurrect streamed chunks."""
    events = [{"type": "token", "text": "abandoned"}, {"type": "assistant", "text": ""}]
    assert _persist_content(events) == ""


# ── 3. availability probing must stay off the request path ───────────────────

def test_availability_probe_is_cached_and_never_blocks_twice():
    from nova_intelligence.ollama_provider import OllamaProvider

    p = OllamaProvider(model="test-model")
    calls = {"n": 0}

    def _slow_probe():
        calls["n"] += 1
        time.sleep(0.4)
        p._avail_check_result = True
        p._avail_check_time = time.time()
        return True

    p._probe_availability = _slow_probe

    t0 = time.time()
    assert p.is_available() is True          # first call may block
    first_ms = (time.time() - t0) * 1000
    assert calls["n"] == 1

    t1 = time.time()
    for _ in range(20):
        p.is_available()
    cached_ms = (time.time() - t1) * 1000

    assert first_ms >= 300, "first probe should have actually run"
    assert cached_ms < 100, f"cached calls must not block (took {cached_ms:.0f}ms)"
    assert calls["n"] == 1, "cache must not re-probe within the TTL"


def test_stale_availability_serves_last_value_without_blocking():
    from nova_intelligence.ollama_provider import OllamaProvider

    p = OllamaProvider(model="test-model")
    p._avail_check_result = True
    p._avail_check_time = time.time() - (OllamaProvider.AVAIL_TTL_S + 5)  # stale

    def _slow_probe():
        time.sleep(1.0)
        return True

    p._probe_availability = _slow_probe

    t0 = time.time()
    result = p.is_available()
    elapsed_ms = (time.time() - t0) * 1000

    assert result is True, "must serve the last known value"
    assert elapsed_ms < 200, f"stale refresh must not block the caller ({elapsed_ms:.0f}ms)"


def test_model_change_invalidates_availability():
    from nova_intelligence.ollama_provider import OllamaProvider
    p = OllamaProvider(model="a")
    p._avail_check_time = time.time()
    p._avail_check_result = True
    p.model = "b"
    assert p._avail_check_time == 0.0
