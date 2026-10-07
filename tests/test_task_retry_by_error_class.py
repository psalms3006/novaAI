"""Background steps retry what waiting can fix, and nothing else.

Before: every exception got exactly one retry, a tool that *returned*
"timed out" got none, and a refusal or a missing module was retried as if
waiting could change it. Failures are now classified (nova_core.errors).
"""
from __future__ import annotations

import time

from task_manager import TaskManager

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "PARTIALLY_COMPLETED", "UNVERIFIED"}


def _run(tmp_path, executor):
    tm = TaskManager(path=str(tmp_path / "tasks.json"), tool_executor=executor)
    tm.RETRY_DELAYS_S = (0.01, 0.01)
    task = tm.submit("t", [{"tool": "web_search", "args": {"query": "x"}}])
    tm.start()
    try:
        deadline = time.time() + 10
        while task.status not in TERMINAL and time.time() < deadline:
            time.sleep(0.02)
    finally:
        tm.stop()
    return task, task.steps[0]


def test_a_timeout_returned_as_text_is_retried_until_it_works(tmp_path):
    calls = []

    def ex(tool, args, meta):
        calls.append(1)
        return "Error: the search timed out" if len(calls) < 3 else "Found 3 results about x"

    task, step = _run(tmp_path, ex)
    assert len(calls) == 3
    assert step.retries == 2
    assert "Found 3 results" in step.result
    assert step.status != "FAILED"
    retries = [h for h in task.history if h["event"] == "task.retry"]
    assert len(retries) == 2 and "temporary" in retries[0]["detail"]


def test_a_refusal_is_not_retried(tmp_path):
    calls = []

    def ex(tool, args, meta):
        calls.append(1)
        return "Refused: browser_read is turned off in your settings."

    task, step = _run(tmp_path, ex)
    assert len(calls) == 1
    assert step.error_class == "permission"
    assert step.status == "FAILED"


def test_a_missing_module_is_not_retried(tmp_path):
    calls = []

    def ex(tool, args, meta):
        calls.append(1)
        raise ModuleNotFoundError("No module named 'duckduckgo_search'")

    task, step = _run(tmp_path, ex)
    assert len(calls) == 1
    assert step.error_class == "unavailable"
    assert task.status == "FAILED"


def test_a_transient_that_never_clears_stops_after_the_budget(tmp_path):
    calls = []

    def ex(tool, args, meta):
        calls.append(1)
        raise ConnectionError("connection reset by peer")

    task, step = _run(tmp_path, ex)
    assert len(calls) == 3
    assert step.error_class == "transient"
    assert task.status == "FAILED"
