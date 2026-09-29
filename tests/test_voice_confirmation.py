"""Asking permission through a channel the person can actually answer.

From a real terminal session:

    23:45:43  GATE: awaiting confirmation for computer_settings({"action": "shutdown"})
    23:45:57  DECLINED: computer_settings — user said: ''
    23:45:58  GATE: awaiting confirmation for computer_settings({"action": "lock"})
    23:46:14  DECLINED: computer_settings — user said: ''

The gate did its job: it refused to shut the machine down. But the question
was asked with `input()` on stdin, while the user was talking. It was printed
to a terminal nobody was watching, NOVA went silent for fifteen seconds
waiting to be typed at, and then took the silence as "no".

From the user's side NOVA simply stopped responding. A voice-first assistant
asked a question through a channel its user cannot reply through.

So: when a live voice session exists, the question is spoken into it and the
answer is the next thing the person says. Falling back to stdin only when
there is no voice session to use.
"""
from __future__ import annotations

import queue
import time

import pytest

from nova_confirm import VoiceConfirmer


class FakeLive:
    """Enough of LiveManager to stand in for one."""

    def __init__(self, state="connected"):
        self._state = state
        self.spoken = []
        self._subs = []

    def status(self):
        return {"state": self._state}

    def send_text(self, text):
        self.spoken.append(text)
        return {"ok": True}

    def subscribe(self):
        q = queue.Queue(maxsize=50)
        self._subs.append(q)
        return q

    def unsubscribe(self, q):
        if q in self._subs:
            self._subs.remove(q)

    def say_back(self, text):
        """Simulate the user replying out loud."""
        for q in self._subs:
            q.put(_Event("user_transcript", text=text))


class _Event:
    def __init__(self, type_, **data):
        self.type = type_
        self.data = data


def _confirmer(live, timeout=2.0):
    c = VoiceConfirmer(live_factory=lambda: live, timeout_seconds=timeout)
    return c


def test_the_question_is_spoken_into_the_live_session():
    live = FakeLive()
    confirmer = _confirmer(live)

    confirmer.speak("I'm about to shut down this computer. Should I proceed?")

    assert live.spoken, "the question was never spoken"
    assert "shut down" in live.spoken[0].lower()


def test_the_model_is_told_to_relay_the_question_not_answer_it():
    """From a real session (2026-09-23): a computer_settings confirmation
    looped for three-plus minutes, ~20.5s per attempt (exactly
    DEFAULT_TIMEOUT_S) -- the user never got a real chance to answer.

    send_text() delivers the text to desk/live_session.py as a "role":
    "user" turn -- content the model treats as something the user just said
    to it. Sending the bare question ("I'm about to X. Should I proceed?")
    reads exactly like the user asking the model for permission, so Gemini
    answered its own question instead of relaying it to the real person.
    The greeting uses the identical mechanism and works, because it sends
    an instruction ("say this out loud to the user") rather than content to
    react to -- speak() must do the same.
    """
    live = FakeLive()
    confirmer = _confirmer(live)

    confirmer.speak("I'm about to shut down this computer. Should I proceed?")

    sent = live.spoken[0].lower()
    assert "shut down" in sent  # the actual question must still be in there
    assert "ask the user" in sent or "out loud" in sent, (
        "the model was not instructed to voice the question to the user -- "
        "sent as bare content, Gemini answers it itself instead of relaying "
        "it, which is why no one ever heard a real question to reply to"
    )
    assert "not" in sent or "wait" in sent, (
        "nothing tells the model to stop and wait for a real answer rather "
        "than continuing on its own"
    )


def test_a_spoken_yes_is_the_answer():
    live = FakeLive()
    confirmer = _confirmer(live)

    import threading
    threading.Timer(0.05, lambda: live.say_back("yes, go ahead")).start()

    assert "yes" in confirmer.ask().lower()


def test_a_spoken_no_is_the_answer():
    live = FakeLive()
    confirmer = _confirmer(live)

    import threading
    threading.Timer(0.05, lambda: live.say_back("no, don't")).start()

    assert "no" in confirmer.ask().lower()


def test_silence_declines_rather_than_waiting_forever():
    live = FakeLive()
    confirmer = _confirmer(live, timeout=0.2)

    started = time.time()
    answer = confirmer.ask()
    elapsed = time.time() - started

    assert answer == "", "silence was read as something other than no"
    assert elapsed < 2.0, f"waited {elapsed:.1f}s; NOVA appears frozen"


def test_the_user_is_told_when_a_silent_decline_happens():
    """Otherwise NOVA just goes quiet, which is what happened."""
    live = FakeLive()
    confirmer = _confirmer(live, timeout=0.2)
    confirmer.ask()

    combined = " ".join(live.spoken).lower()
    assert "didn't" in combined or "did not" in combined or "cancel" in combined, (
        f"NOVA declined silently and said nothing about it: {live.spoken}"
    )


def test_without_a_voice_session_it_falls_back_to_typing(monkeypatch):
    confirmer = VoiceConfirmer(live_factory=lambda: None, timeout_seconds=0.2)
    monkeypatch.setattr("builtins.input", lambda *a: "yes")
    assert confirmer.ask() == "yes"


def test_a_disconnected_session_is_not_used(monkeypatch):
    live = FakeLive(state="closed")
    confirmer = _confirmer(live, timeout=0.2)
    monkeypatch.setattr("builtins.input", lambda *a: "typed")

    assert confirmer.ask() == "typed"
    assert not live.spoken, "spoke into a session that was not connected"


def test_a_broken_session_does_not_raise_into_the_gate(monkeypatch):
    class Broken:
        def status(self):
            raise RuntimeError("gone")

    confirmer = VoiceConfirmer(live_factory=lambda: Broken(),
                               timeout_seconds=0.2)
    monkeypatch.setattr("builtins.input", lambda *a: "no")
    assert confirmer.ask() == "no"


def test_stale_speech_from_before_the_question_is_ignored():
    """The answer is what they said *after* being asked, not before."""
    live = FakeLive()
    confirmer = _confirmer(live, timeout=0.3)

    live.say_back("what's the weather")       # said before subscribing
    import threading
    threading.Timer(0.05, lambda: live.say_back("yes")).start()

    answer = confirmer.ask()
    assert "weather" not in answer.lower(), (
        f"an earlier utterance was taken as the answer: {answer!r}"
    )


def test_it_unsubscribes_when_done():
    """A listener left behind would collect every utterance forever."""
    live = FakeLive()
    confirmer = _confirmer(live, timeout=0.2)
    confirmer.ask()
    assert live._subs == [], "the event listener was never removed"


def test_the_dispatcher_asks_through_the_voice_channel():
    """The wiring. Without it the fix exists and is never used."""
    import inspect

    import nova

    # Every call goes _execute_tool_sync (hooks) -> _execute_tool_core (gates).
    assert "_execute_tool_core(" in inspect.getsource(nova._execute_tool_sync)
    source = inspect.getsource(nova._execute_tool_core)
    assert "VoiceConfirmer" in source, (
        "the confirmation gate still asks on stdin during a voice session"
    )
    assert "speak_fn=" in source, (
        "the question is never spoken, so the user cannot know they were asked"
    )
    assert 'input("NOVA awaiting' not in source, (
        "stdin is still the unconditional channel"
    )
