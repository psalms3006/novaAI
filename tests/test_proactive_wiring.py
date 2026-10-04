"""A finished background task has to reach the user on the desktop surface.

`task_manager.set_notify` was wired in exactly two places, `nova.py`'s
terminal path and its offline loop. The desktop app -- the one people actually
run -- wired neither, so a background task could complete and say nothing at
all. The proactive agent that was supposed to carry such notices was a stub.

These tests pin the wiring, not the policy (tests/test_proactive_policy.py
covers the policy).
"""
from __future__ import annotations

import nova
import nova_proactive


def test_nova_uses_the_real_proactive_agent_not_the_stub():
    """The stub's start() set a flag and returned; it could never speak."""
    assert nova.ProactiveAgent is nova_proactive.ProactiveAgent
    assert nova.ProactiveEvent is not None
    assert nova.Priority is not None


def test_the_desktop_path_wires_task_notices_to_the_proactive_agent():
    """Fails if `_start_ambient_intelligence` forgets the task manager again."""
    import inspect

    source = inspect.getsource(nova._start_ambient_intelligence)
    assert "set_notify" in source, (
        "the desktop surface does not route background task outcomes "
        "anywhere; a finished task would say nothing"
    )
    assert "set_busy" in source, (
        "nothing tells the proactive agent when NOVA is mid-sentence, so a "
        "notice can talk over the answer the user asked for"
    )


def test_a_task_outcome_travels_from_the_task_manager_to_a_voice(tmp_path):
    """The whole path, with a recorder standing in for the speaker."""
    from task_manager import TaskManager

    said = []
    agent = nova_proactive.ProactiveAgent(speak_fn=said.append)

    def notice(text):
        agent.emit(nova_proactive.ProactiveEvent(
            kind="completion",
            priority=nova_proactive.Priority.HIGH,
            message=text,
        ))

    manager = TaskManager(
        path=str(tmp_path / "tasks.json"),
        tool_executor=lambda tool, args, meta: "Saved: report.docx",
    )
    manager.set_notify(notice)

    task = manager.submit(
        "Write the report",
        [{"tool": "generate_document", "args": {"name": "report"}}],
    )

    import time
    manager.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not said:
            time.sleep(0.02)
    finally:
        manager.stop()

    assert said, "the task finished and the user was never told"
    assert task.status == "COMPLETED", task.reason_for_stop


def test_a_broken_speaker_does_not_break_the_task_worker():
    """emit() runs on the task manager's worker thread."""
    agent = nova_proactive.ProactiveAgent(
        speak_fn=lambda _t: (_ for _ in ()).throw(RuntimeError("no speaker"))
    )
    # Must return, not raise.
    assert agent.emit(nova_proactive.ProactiveEvent(
        kind="completion", priority=nova_proactive.Priority.HIGH,
        message="Done.",
    )) is False
