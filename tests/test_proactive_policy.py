"""When is NOVA allowed to speak without being spoken to?

The plumbing for unprompted speech already existed and was correct:
`nova._start_ambient_intelligence` builds a `_speak` that hands text to the
live Gemini session when one is connected (`mgr.send_text`), and falls back to
the transcript UI otherwise — one session, never a second one.

What did not exist was anything deciding *whether* to speak. `ProactiveAgent`
was a stub whose `start()` set a flag and returned, so "[DESK] proactive agent
started" appeared in the log of an assistant that could not say anything on its
own initiative.

The risk in fixing that is the opposite failure: an assistant that narrates.
"I'm still researching" three times is worse than silence. So the policy is
tested from both sides — important things must get through, and ordinary
progress must not.
"""
from __future__ import annotations

import pytest

from nova_proactive import ProactiveAgent, ProactiveEvent, Priority


@pytest.fixture
def spoken():
    """An agent wired to a recorder instead of a voice."""
    said = []
    agent = ProactiveAgent(speak_fn=said.append)
    agent._now = lambda: 1000.0          # frozen clock; tests advance it
    return agent, said


def _advance(agent, seconds):
    base = agent._now()
    agent._now = lambda: base + seconds


def test_a_safety_warning_is_always_spoken(spoken):
    agent, said = spoken
    agent.emit(ProactiveEvent(
        kind="safety", priority=Priority.CRITICAL,
        message="That command deletes your home directory.",
    ))
    assert said == ["That command deletes your home directory."]


def test_routine_progress_is_never_spoken(spoken):
    """The "I'm still researching" failure mode."""
    agent, said = spoken
    for _ in range(3):
        agent.emit(ProactiveEvent(
            kind="progress", priority=Priority.LOW,
            message="Still researching.",
        ))
    assert said == []


def test_a_finished_task_is_reported(spoken):
    agent, said = spoken
    agent.emit(ProactiveEvent(
        kind="completion", priority=Priority.HIGH,
        message="I've finished the research on AI developments.",
        task_id="T-1",
    ))
    assert len(said) == 1


def test_the_same_finding_is_not_announced_twice(spoken):
    agent, said = spoken
    for _ in range(3):
        agent.emit(ProactiveEvent(
            kind="discovery", priority=Priority.MEDIUM,
            message="I found a paper relevant to your assignment.",
            dedupe_key="paper-123",
        ))
    assert len(said) == 1, f"said {len(said)} times: {said}"


def test_ordinary_findings_are_spaced_out(spoken):
    """Two unrelated discoveries in quick succession: one now, one held."""
    agent, said = spoken
    agent.emit(ProactiveEvent(kind="discovery", priority=Priority.MEDIUM,
                              message="First finding.", dedupe_key="a"))
    agent.emit(ProactiveEvent(kind="discovery", priority=Priority.MEDIUM,
                              message="Second finding.", dedupe_key="b"))
    assert said == ["First finding."]

    _advance(agent, 600)
    agent.emit(ProactiveEvent(kind="discovery", priority=Priority.MEDIUM,
                              message="Third finding.", dedupe_key="c"))
    assert said == ["First finding.", "Third finding."]


def test_urgency_is_not_delayed_by_the_spacing_rule(spoken):
    """A safety warning must not wait behind a chatty discovery."""
    agent, said = spoken
    agent.emit(ProactiveEvent(kind="discovery", priority=Priority.MEDIUM,
                              message="A finding.", dedupe_key="a"))
    agent.emit(ProactiveEvent(kind="safety", priority=Priority.CRITICAL,
                              message="Careful — that looks like an API key."))
    assert said == ["A finding.", "Careful — that looks like an API key."]


def test_nothing_is_said_while_nova_is_already_talking(spoken):
    """Do not talk over the answer the user actually asked for."""
    agent, said = spoken
    agent.set_busy(lambda: True)
    agent.emit(ProactiveEvent(kind="completion", priority=Priority.HIGH,
                              message="Research finished."))
    assert said == []

    agent.set_busy(lambda: False)
    agent.flush()
    assert said == ["Research finished."]


def test_a_held_message_is_not_lost_when_a_newer_one_arrives(spoken):
    agent, said = spoken
    agent.set_busy(lambda: True)
    agent.emit(ProactiveEvent(kind="completion", priority=Priority.HIGH,
                              message="First task done."))
    agent.emit(ProactiveEvent(kind="completion", priority=Priority.HIGH,
                              message="Second task done."))
    agent.set_busy(lambda: False)
    agent.flush()
    assert said == ["First task done.", "Second task done."]


def test_the_queue_is_bounded(spoken):
    """An unattended agent must not accumulate messages forever."""
    agent, said = spoken
    agent.set_busy(lambda: True)
    for i in range(500):
        agent.emit(ProactiveEvent(kind="completion", priority=Priority.HIGH,
                                  message=f"Message {i}.", dedupe_key=f"k{i}"))
    assert len(agent._queue) <= agent.MAX_QUEUE


def test_a_failing_voice_does_not_bring_down_the_caller(spoken):
    """This runs on the task worker thread; it must never raise into it."""
    agent, _ = spoken

    def broken(_text):
        raise RuntimeError("speaker unplugged")

    agent.speak_fn = broken
    agent.emit(ProactiveEvent(kind="safety", priority=Priority.CRITICAL,
                              message="Anything."))   # must not raise


def test_an_event_needing_an_answer_is_marked_as_such(spoken):
    """Memory consent rides this path: "should I remember that?"."""
    agent, said = spoken
    agent.emit(ProactiveEvent(
        kind="needs_input", priority=Priority.HIGH,
        message="Should I remember that for future sessions?",
        requires_response=True,
    ))
    assert said == ["Should I remember that for future sessions?"]
    assert agent.awaiting_response() is True
