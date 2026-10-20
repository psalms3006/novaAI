"""Regression tests for NOVA task recovery after interruption.

The recovery contract (task_manager._load): a task that was mid-flight when
NOVA stopped is FAILED with an explicit reason — it is never silently
resumed, because resuming would re-run steps whose side effects already
happened. Terminal tasks are untouched. These tests pin that contract from
both sides: interrupted work is handled predictably, and completed/failed/
cancelled work survives restarts exactly as it was.

Recovery here means *persistence recovery* — recreate the manager from the
same file and verify what came back. Nothing in these tests accepts an
in-memory reset as evidence.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from task_manager import (
    TaskManager,
    Task,
    TaskStep,
    ACTIVE,
    TERMINAL,
)


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_tasks_file(tmp_path):
    return str(tmp_path / "tasks.json")


@pytest.fixture
def mock_executor():
    """Tool executor that returns success for web_search by default."""
    return Mock(return_value="Found 5 results for query: test")


@pytest.fixture
def mock_generate_doc_executor(tmp_path):
    """Tool executor that creates the real file generate_document's
    verification checks for."""
    def executor(tool, args, meta):
        if tool == "generate_document":
            title = args.get("title", "output")
            path = tmp_path / f"{title}.txt"
            path.write_text("Test content", encoding="utf-8")
            return f"Saved: {path} (100 bytes)"
        return "Found results for: " + str(args.get("query", ""))
    return Mock(side_effect=executor)


def _run_task_to_completion(tm: TaskManager, task: Task, timeout: float = 10.0) -> Task:
    terminal = set(TERMINAL)
    tm.start()
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if task.status in terminal:
                return task
            time.sleep(0.02)
        pytest.fail(f"Task {task.id} never reached terminal state (stuck in {task.status})")
    finally:
        tm.stop()


def _write_state(path: str, *tasks: Task) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([t.to_dict() for t in tasks], f)


# ── Test Group 1: interrupted ACTIVE tasks fail loudly ──────────────────────

class TestInterruptedTasksFailLoudly:
    """A task mid-flight at shutdown is FAILED with a reason, never
    silently re-run: the steps whose side effects already happened must not
    happen again just because the process came back."""

    def test_running_task_fails_with_reason_on_reload(self, tmp_tasks_file, mock_executor):
        task = Task(title="Recovery Test", steps=[
            TaskStep(tool="web_search", args={"query": "test"})
        ])
        task.status = "RUNNING"
        task.current_step = 0
        task.steps[0].status = "RUNNING"
        task.steps[0].started = time.time()
        _write_state(tmp_tasks_file, task)

        tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_executor)
        loaded = tm.get(task.id)

        assert loaded is not None
        assert loaded.status == "FAILED"
        assert "closed" in loaded.reason_for_stop.lower() or \
            "running" in loaded.reason_for_stop.lower()
        assert loaded.finished > 0

    def test_waiting_task_fails_on_reload(self, tmp_tasks_file, mock_executor):
        task = Task(title="Waiting Test", steps=[
            TaskStep(tool="open_app", args={"app_name": "notepad"})
        ])
        task.status = "WAITING"
        task.current_step = 0
        task.steps[0].status = "WAITING"
        _write_state(tmp_tasks_file, task)

        tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_executor)
        loaded = tm.get(task.id)
        assert loaded.status == "FAILED"

    def test_planning_task_fails_on_reload(self, tmp_tasks_file, mock_executor):
        """PLANNING is ACTIVE: it gets the same treatment. A task whose
        plan was never built has nothing to resume anyway."""
        task = Task(title="Planning Test", user_request="Do something",
                    needs_plan=True)
        task.status = "PLANNING"
        _write_state(tmp_tasks_file, task)

        tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_executor)
        loaded = tm.get(task.id)
        assert loaded.status == "FAILED"

    def test_interrupted_task_is_never_re_executed(self, tmp_tasks_file):
        """The duplicate-execution guarantee: a task interrupted mid-flight
        does not run again on restart. The executor must not fire."""
        calls = []

        def counting_executor(tool, args, meta):
            calls.append(tool)
            return "Found results"

        task = Task(title="No Rerun", steps=[
            TaskStep(tool="web_search", args={"query": "test"})
        ])
        task.status = "RUNNING"
        task.current_step = 0
        task.steps[0].status = "RUNNING"
        _write_state(tmp_tasks_file, task)

        tm = TaskManager(path=tmp_tasks_file, tool_executor=counting_executor)
        tm.start()
        time.sleep(0.3)
        tm.stop()

        assert calls == [], "an interrupted task re-executed on restart"

    def test_verified_step_evidence_survives(self, tmp_tasks_file, mock_executor):
        """The FAILED verdict is about the task, not an erasure of what
        completed: a VERIFIED step keeps its result and timestamps."""
        task = Task(title="Verified Test", steps=[
            TaskStep(tool="generate_document", args={"title": "done"})
        ])
        task.status = "RUNNING"
        task.current_step = 0
        task.steps[0].status = "VERIFIED"
        task.steps[0].verified = True
        task.steps[0].verification = "CONFIRMED_SUCCESS"
        task.steps[0].finished = 12345.0
        _write_state(tmp_tasks_file, task)

        tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_executor)
        loaded = tm.get(task.id)

        assert loaded.status == "FAILED"          # the task did not finish
        assert loaded.steps[0].status == "VERIFIED"   # the work did
        assert loaded.steps[0].verification == "CONFIRMED_SUCCESS"
        assert loaded.steps[0].finished == 12345.0

    def test_interrupted_task_can_be_retried_as_new_task(self, tmp_tasks_file, mock_generate_doc_executor):
        """The recovery path for interrupted work is the explicit retry:
        a NEW task with its own history, the failed run kept on record."""
        task = Task(title="Interrupted", steps=[
            TaskStep(tool="generate_document", args={"title": "test"})
        ])
        task.status = "RUNNING"
        task.current_step = 0
        task.steps[0].status = "RUNNING"
        _write_state(tmp_tasks_file, task)

        tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_generate_doc_executor)
        loaded = tm.get(task.id)
        assert loaded.status == "FAILED"

        new_task = tm.retry(task.id)
        assert new_task is not None
        assert new_task.id != task.id
        assert new_task.parent_task_id == task.id
        assert new_task.status == "QUEUED"
        assert loaded.status == "FAILED"            # the record stays


# ── Test Group 2: terminal tasks survive restarts unchanged ─────────────────

class TestTerminalSurvival:
    def _isolation_case(self, status: str, **extra) -> Task:
        task = Task(title=f"{status} Task", steps=[
            TaskStep(tool="generate_document", args={"title": status.lower()})
        ])
        task.status = status
        task.reason_for_stop = f"settled as {status}"
        task.finished = time.time()
        task.steps[0].status = "VERIFIED" if status == "COMPLETED" else status
        for k, v in extra.items():
            setattr(task, k, v)
        return task

    @pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "CANCELLED",
                                        "PARTIALLY_COMPLETED", "UNVERIFIED"])
    def test_terminal_task_unchanged_after_reload(self, tmp_tasks_file,
                                                  mock_executor, status):
        settled = self._isolation_case(status, progress=100 if status == "COMPLETED" else 0)
        running = Task(title="Running Task", steps=[
            TaskStep(tool="generate_document", args={"title": "running"})
        ])
        running.status = "RUNNING"
        running.current_step = 0
        running.steps[0].status = "RUNNING"
        running.steps[0].started = time.time()
        _write_state(tmp_tasks_file, settled, running)

        tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_executor)

        loaded_settled = tm.get(settled.id)
        loaded_running = tm.get(running.id)

        assert loaded_settled.status == status, \
            f"{status} task was disturbed by recovery"
        assert loaded_settled.reason_for_stop == f"settled as {status}"
        assert loaded_settled.steps[0].status == settled.steps[0].status
        # ...and the interrupted one beside it got the new contract:
        assert loaded_running.status == "FAILED"

    def test_recovery_does_not_corrupt_unrelated_tasks(self, tmp_tasks_file, mock_executor):
        completed = self._isolation_case("COMPLETED", progress=100)
        running = Task(title="Running Task", steps=[
            TaskStep(tool="generate_document", args={"title": "running"})
        ])
        running.status = "RUNNING"
        running.current_step = 0
        running.steps[0].status = "RUNNING"
        running.steps[0].started = time.time()
        _write_state(tmp_tasks_file, completed, running)

        tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_executor)

        loaded_completed = tm.get(completed.id)
        loaded_running = tm.get(running.id)

        assert loaded_completed.status == "COMPLETED"
        assert loaded_completed.progress == 100
        assert loaded_completed.steps[0].status == "VERIFIED"
        assert loaded_running.status == "FAILED"

    def test_recovery_is_idempotent(self, tmp_tasks_file, mock_executor):
        """Reloading the same file N times gives the same answer N times —
        and reloading does not change the file (a second manager sees the
        same state the first wrote)."""
        task = Task(title="Repeated Recovery", steps=[
            TaskStep(tool="generate_document", args={"title": "test"})
        ])
        task.status = "RUNNING"
        task.current_step = 0
        task.steps[0].status = "RUNNING"
        _write_state(tmp_tasks_file, task)

        before = open(tmp_tasks_file, encoding="utf-8").read()
        for _ in range(3):
            tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_executor)
            loaded = tm.get(task.id)
            assert loaded.status == "FAILED"
        after = open(tmp_tasks_file, encoding="utf-8").read()
        assert before == after, "merely reading the state file changed it"

    def test_completed_task_never_re_executes(self, tmp_tasks_file):
        calls = []
        task = Task(title="Done Already", steps=[
            TaskStep(tool="generate_document", args={"title": "done"})
        ])
        task.status = "COMPLETED"
        task.progress = 100
        task.steps[0].status = "VERIFIED"
        _write_state(tmp_tasks_file, task)

        tm = TaskManager(path=tmp_tasks_file,
                         tool_executor=lambda t, a, m: calls.append(t) or "ok")
        tm.start()
        time.sleep(0.3)
        tm.stop()
        assert calls == []


# ── Test Group 3: full lifecycle through a restart ──────────────────────────

class TestLifecycleAcrossRestart:
    def test_completed_work_reload_clean(self, tmp_tasks_file, mock_generate_doc_executor):
        """The positive path: a task that finishes before the restart is
        COMPLETED after it, with every field it earned."""
        tm1 = TaskManager(path=tmp_tasks_file, tool_executor=mock_generate_doc_executor)
        task = tm1.submit("Partial", [
            {"tool": "generate_document", "args": {"title": "step1"}},
            {"tool": "generate_document", "args": {"title": "step2"}},
        ])
        _run_task_to_completion(tm1, task)

        assert task.status == "COMPLETED"
        assert all(s.status == "VERIFIED" for s in task.steps)
        started, finished = task.steps[0].started, task.steps[0].finished

        tm2 = TaskManager(path=tmp_tasks_file, tool_executor=mock_generate_doc_executor)
        loaded = tm2.get(task.id)

        assert loaded.status == "COMPLETED"
        assert loaded.progress == 100
        assert all(s.status == "VERIFIED" for s in loaded.steps)
        assert loaded.steps[0].started == started
        assert loaded.steps[0].finished == finished

    def test_failed_run_then_retry_then_success(self, tmp_tasks_file, mock_generate_doc_executor):
        """A failed task is retried explicitly as a new task, which then
        succeeds — the full recovery loop."""
        tm = TaskManager(path=tmp_tasks_file,
                         tool_executor=Mock(side_effect=TimeoutError("fail")))
        task = tm.submit("Flaky", [{"tool": "generate_document",
                                    "args": {"title": "out"}}])
        _run_task_to_completion(tm, task)
        assert task.status == "FAILED"

        # The retry runs against a working executor.
        tm.set_tool_executor(mock_generate_doc_executor)
        new_task = tm.retry(task.id)
        assert new_task is not None
        _run_task_to_completion(tm, new_task)

        assert new_task.status == "COMPLETED"
        assert task.status == "FAILED"            # both records kept
        assert new_task.parent_task_id == task.id


# ── Test Group 4: missing / corrupted state ─────────────────────────────────

class TestMissingCorruptedState:
    def test_missing_tasks_file_starts_empty(self, tmp_path, mock_executor):
        tm = TaskManager(path=str(tmp_path / "nonexistent.json"),
                         tool_executor=mock_executor)
        assert tm.list() == []

    def test_corrupted_json_starts_empty(self, tmp_tasks_file, mock_executor):
        with open(tmp_tasks_file, "w") as f:
            f.write("{ invalid json")
        tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_executor)
        assert tm.list() == []

    def test_corrupted_entry_does_not_poison_valid_ones(self, tmp_tasks_file, mock_executor):
        """One unreadable task must not take the whole history with it."""
        good = Task(title="Good", steps=[
            TaskStep(tool="web_search", args={"query": "test"})])
        good.status = "COMPLETED"
        with open(tmp_tasks_file, "w", encoding="utf-8") as f:
            json.dump([good.to_dict(), {"not": "a task"}], f)
        tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_executor)
        assert any(t.title == "Good" for t in tm.list())

    def test_extra_fields_ignored(self, tmp_tasks_file, mock_executor):
        task = Task(title="Extra Fields", steps=[
            TaskStep(tool="web_search", args={"query": "test"})])
        task.status = "COMPLETED"
        d = task.to_dict()
        d["future_field"] = {"anything": True}
        d["steps"][0]["step_future"] = 1
        _write_state(tmp_tasks_file, Task.from_dict(d))
        tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_executor)
        loaded = tm.get(task.id)
        assert loaded is not None and loaded.title == "Extra Fields"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
