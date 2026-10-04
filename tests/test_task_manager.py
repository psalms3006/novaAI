"""The AIOS task manager: what it does when a task arrives underspecified.

A real run on 2026-09-19 left `nova_tasks_aios.json` holding a single task
called "Untitled task" whose one step was refused:

    Refused: tool 'agent' declares no capabilities, so it cannot be authorised.

Nothing was wrong with the permission engine — it fail-closed on a tool that
has never existed in NOVA. The tool name was invented by `submit()` as a
placeholder when it was handed no steps. The task then finished as UNVERIFIED
("steps ran but success could not be verified"), which was spoken to the user
and written into living memory as a recallable fact.

These tests pin the three things that went wrong: a fabricated tool name, a
submission with nothing in it being accepted, and a categorical refusal being
reported as an unverified success.
"""
from __future__ import annotations

import re
import time

import pytest

from task_manager import TaskManager

#: The refusal `nova.py` returns when the permission engine denies a tool.
#: Copied verbatim from the artifact that prompted these tests.
REFUSAL = ("Refused: tool 'agent' declares no capabilities, so it cannot be "
           "authorised.")


def _manager(tmp_path, executor):
    return TaskManager(path=str(tmp_path / "tasks.json"), tool_executor=executor)


def _run_to_completion(tm, task, timeout=10.0):
    """Start the worker, wait for a terminal status, stop. Returns the task."""
    terminal = {"COMPLETED", "FAILED", "CANCELLED", "PARTIALLY_COMPLETED",
                "UNVERIFIED"}
    tm.start()
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if task.status in terminal:
                return task
            time.sleep(0.02)
        pytest.fail(f"task never reached a terminal status (stuck in {task.status})")
    finally:
        tm.stop()


def test_a_task_submitted_with_no_steps_is_refused(tmp_path):
    """Submitting nothing must not invent something to run.

    Fails if `submit()` substitutes a placeholder step instead of rejecting
    the call.
    """
    tm = _manager(tmp_path, lambda tool, args, meta: "ok")
    with pytest.raises(ValueError) as excinfo:
        tm.submit("Write my report", [])
    assert "step" in str(excinfo.value).lower()
    assert tm.list() == []


def test_submit_without_steps_tells_the_model_what_is_missing(tmp_path):
    """The model's own route in. It should be told, not silently queued.

    Fails if `exec_command("submit")` reports a task as queued.
    """
    tm = _manager(tmp_path, lambda tool, args, meta: "ok")
    reply = tm.exec_command("submit")
    assert "queued" not in reply.lower()
    assert "steps" in reply.lower()
    assert tm.list() == []


def test_no_tool_name_the_task_manager_can_submit_is_undeclared():
    """The drift guard that would have caught this.

    `tests/test_permissions.py` already asserts every tool `nova.py` dispatches
    is declared, but it only looks at `tool_name == "..."` branches in nova.py.
    The fabricated name lived in a step dict in task_manager.py, where that
    regex could never see it.

    Fails if task_manager.py hardcodes a step naming a tool with no
    capabilities.
    """
    from nova_core.permissions import capabilities_for_tool

    with open("task_manager.py", encoding="utf-8") as handle:
        source = handle.read()

    named = set(re.findall(r'["\']tool["\']\s*:\s*["\']([a-z_]+)["\']', source))
    undeclared = sorted(t for t in named if capabilities_for_tool(t) is None)
    assert undeclared == [], (
        f"task_manager.py can submit steps naming undeclared tool(s): "
        f"{undeclared}. A step naming a tool NOVA cannot dispatch is refused "
        f"at execution time and reported to the user as an unverified task."
    )


def test_a_refused_step_fails_the_task_instead_of_reading_as_unverified(tmp_path):
    """A denial is a failure, not an inconclusive result.

    "Refused: ..." matched none of the failure markers ("denied", "blocked",
    ...), so the step verified as UNKNOWN and the task ended UNVERIFIED with
    "steps ran but success could not be verified" — which is not what
    happened. Nothing ran.

    Fails if the refusal is not recognised as a failure.
    """
    tm = _manager(tmp_path, lambda tool, args, meta: REFUSAL)
    task = tm.submit("Read my screen", [{"tool": "screen_read", "args": {}}])
    _run_to_completion(tm, task)

    assert task.status == "FAILED", (
        f"a refused step left the task {task.status} "
        f"({task.reason_for_stop!r})"
    )
    assert task.steps[0].verification == "FAILURE"


def test_a_step_that_really_succeeds_still_completes(tmp_path):
    """Guard against fixing the above by calling everything a failure."""
    tm = _manager(tmp_path, lambda tool, args, meta: "Saved: report.docx")
    task = tm.submit("Make the report",
                     [{"tool": "generate_document", "args": {"name": "report"}}])
    _run_to_completion(tm, task)

    assert task.status == "COMPLETED", f"reason: {task.reason_for_stop!r}"
    assert task.steps[0].verification == "CONFIRMED_SUCCESS"
