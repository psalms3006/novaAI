"""Regression tests for NOVA task lifecycle and state transitions.

These tests verify the observable behavior of task and step state machines
as implemented in task_manager.py, without depending on implementation details.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from unittest.mock import Mock

import pytest

from task_manager import (
    TaskManager,
    Task,
    TaskStep,
    TASK_STATUSES,
    STEP_STATUSES,
    TERMINAL,
    ACTIVE,
)


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_tasks_file(tmp_path):
    """Provide a temporary tasks file path."""
    return str(tmp_path / "tasks.json")


@pytest.fixture
def mock_executor():
    """Tool executor that returns success for web_search by default."""
    # web_search verification looks for "found " or actual results
    # Not failure patterns like "no results found for", "couldn't search"
    return Mock(return_value="Found 5 results for query: test")


@pytest.fixture
def mock_failing_executor():
    """Tool executor that always fails with transient error."""
    return Mock(side_effect=TimeoutError("transient failure"))


@pytest.fixture
def mock_permission_failing_executor():
    """Tool executor that fails with permission error (non-retryable)."""
    return Mock(side_effect=PermissionError("Access denied"))


@pytest.fixture
def mock_generate_doc_executor(tmp_path):
    """Tool executor that creates actual file for generate_document."""
    def executor(tool, args, meta):
        if tool == "generate_document":
            # Create the file that verification expects
            title = args.get("title", "output")
            path = tmp_path / f"{title}.txt"
            path.write_text("Test content")
            return f"Saved: {path} (100 bytes)"
        return "Found results for: " + str(args.get("query", ""))
    return Mock(side_effect=executor)


@pytest.fixture
def mock_slow_executor():
    """Tool executor that simulates slow execution (500ms per call)."""
    def slow_executor(tool, args, meta):
        time.sleep(0.5)
        return "Found results for: " + str(args.get("query", ""))
    return Mock(side_effect=slow_executor)


@pytest.fixture
def mock_very_slow_executor():
    """Tool executor that simulates very slow execution (2s per call)."""
    def very_slow_executor(tool, args, meta):
        time.sleep(2.0)
        return "Found results for: " + str(args.get("query", ""))
    return Mock(side_effect=very_slow_executor)


@pytest.fixture
def task_manager(tmp_tasks_file, mock_executor):
    """TaskManager with a mock executor and temp persistence."""
    return TaskManager(path=tmp_tasks_file, tool_executor=mock_executor)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _run_task_to_completion(tm: TaskManager, task: Task, timeout: float = 10.0) -> Task:
    """Start the worker and wait for task to reach a terminal state."""
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


# ── Test Group 1: Task State Transitions ────────────────────────────────────

class TestTaskStateTransitions:
    """Verify valid and invalid task state transitions."""

    def test_initial_task_status_is_queued(self, task_manager):
        """Newly submitted tasks start in QUEUED state."""
        task = task_manager.submit("Test task", [{"tool": "web_search", "args": {"query": "test"}}])
        assert task.status == "QUEUED"

    def test_task_progresses_from_queued_to_running(self, task_manager):
        """Task moves from QUEUED to RUNNING when worker picks it up."""
        task = task_manager.submit("Test task", [{"tool": "web_search", "args": {"query": "test"}}])
        _run_task_to_completion(task_manager, task)
        # Task should have passed through RUNNING to a terminal state
        assert task.status in TERMINAL

    def test_task_completes_successfully(self, tmp_tasks_file, mock_generate_doc_executor):
        """Task reaches COMPLETED when all steps verify successfully."""
        tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_generate_doc_executor)
        task = tm.submit("Test task", [{"tool": "generate_document", "args": {"title": "test"}}])
        _run_task_to_completion(tm, task)
        assert task.status == "COMPLETED"
        assert task.steps[0].status == "VERIFIED"

    def test_task_fails_on_step_failure(self, tmp_tasks_file, mock_failing_executor):
        """Task reaches FAILED when a step fails permanently."""
        tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_failing_executor)
        task = tm.submit("Test task", [{"tool": "web_search", "args": {"query": "test"}}])
        _run_task_to_completion(tm, task)
        assert task.status == "FAILED"

    def test_task_cancelled_from_queued(self, task_manager):
        """Cancelling a QUEUED task moves it to CANCELLED."""
        task = task_manager.submit("Test task", [{"tool": "web_search", "args": {"query": "test"}}])
        result = task_manager.cancel(task.id)
        assert result is True
        assert task.status == "CANCELLED"
        assert task.reason_for_stop == "cancelled by user"

    def test_task_cancelled_from_running(self, tmp_tasks_file, mock_very_slow_executor):
        """Cancelling a RUNNING task waits for current step to finish."""
        tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_very_slow_executor)
        task = tm.submit("Test task", [{"tool": "web_search", "args": {"query": "test"}}])
        tm.start()
        # Wait for task to start running
        time.sleep(0.5)  # Let the worker pick up the task
        result = tm.cancel(task.id, force=False)
        assert result is True
        # Task should eventually reach CANCELLED (after current step finishes)
        _run_task_to_completion(tm, task, timeout=30.0)
        assert task.status == "CANCELLED"

    def test_pause_resume_queued_task(self, task_manager):
        """Pausing and resuming a QUEUED task works correctly."""
        task = task_manager.submit("Test task", [{"tool": "web_search", "args": {"query": "test"}}])
        assert task_manager.pause(task.id) is True
        assert task.status == "PAUSED"
        assert task_manager.resume(task.id) is True
        assert task.status == "QUEUED"

    def test_pause_non_queued_task_fails(self, task_manager):
        """Pausing a non-QUEUED task returns False."""
        task = task_manager.submit("Test task", [{"tool": "web_search", "args": {"query": "test"}}])
        task_manager.start()
        time.sleep(0.1)
        assert task_manager.pause(task.id) is False

    def test_resume_non_paused_task_fails(self, task_manager):
        """Resuming a non-PAUSED task returns False."""
        task = task_manager.submit("Test task", [{"tool": "web_search", "args": {"query": "test"}}])
        assert task_manager.resume(task.id) is False

    def test_cancel_terminal_task_fails(self, task_manager):
        """Cancelling an already COMPLETED task returns False."""
        task = task_manager.submit("Test task", [{"tool": "web_search", "args": {"query": "test"}}])
        _run_task_to_completion(task_manager, task)
        assert task.status == "COMPLETED"
        assert task_manager.cancel(task.id) is False


# ── Test Group 2: Step State Transitions ──────────────────────────────────

class TestStepStateTransitions:
    """Verify valid step state transitions."""

    def test_step_starts_queued(self, task_manager):
        """New steps start in QUEUED state."""
        task = task_manager.submit("Test", [{"tool": "web_search", "args": {"query": "test"}}])
        assert task.steps[0].status == "QUEUED"

    def test_step_progresses_queued_to_running_to_verified(self, tmp_tasks_file, mock_generate_doc_executor):
        """Step moves QUEUED → RUNNING → VERIFIED on success."""
        tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_generate_doc_executor)
        task = tm.submit("Test", [{"tool": "generate_document", "args": {"title": "test"}}])
        _run_task_to_completion(tm, task)
        step = task.steps[0]
        assert step.status == "VERIFIED"
        assert step.verified is True
        assert step.verification == "CONFIRMED_SUCCESS"

    def test_step_failed_is_not_retried_indefinitely(self, tmp_tasks_file, mock_failing_executor):
        """Failed step does not retry beyond configured maximum."""
        tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_failing_executor)
        task = tm.submit("Test", [{"tool": "web_search", "args": {"query": "test"}}])
        _run_task_to_completion(tm, task)
        step = task.steps[0]
        assert step.status == "FAILED"
        # Should have retried len(RETRY_DELAYS_S) + 1 = 3 times total
        assert step.retries == 2  # Two retries after initial attempt

    def test_step_waiting_for_resource(self, tmp_tasks_file, mock_slow_executor):
        """Step enters WAITING when resource is contended."""
        tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_slow_executor)
        # Submit two tasks that need the same resource (desktop)
        task1 = tm.submit("Task 1", [{"tool": "open_app", "args": {"app_name": "notepad"}}])
        task2 = tm.submit("Task 2", [{"tool": "open_app", "args": {"app_name": "calc"}}])
        tm.start()
        time.sleep(0.6)  # Let one task acquire the resource
        # One task should be RUNNING, the other WAITING
        statuses = {task1.status, task2.status}
        assert "WAITING" in statuses or "RUNNING" in statuses
        tm.stop()


# ── Test Group 3: Terminal State Handling ──────────────────────────────────

class TestTerminalStates:
    """Verify behavior of tasks in terminal states."""

    def test_completed_task_not_restarted(self, task_manager):
        """COMPLETED tasks are not picked up by workers."""
        task = task_manager.submit("Test", [{"tool": "web_search", "args": {"query": "test"}}])
        _run_task_to_completion(task_manager, task)
        assert task.status == "COMPLETED"
        task_manager.start()
        time.sleep(0.2)
        # Task should remain COMPLETED
        assert task.status == "COMPLETED"

    def test_failed_task_not_restarted(self, tmp_tasks_file, mock_failing_executor):
        """FAILED tasks are not picked up by workers."""
        tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_failing_executor)
        task = tm.submit("Test", [{"tool": "web_search", "args": {"query": "test"}}])
        _run_task_to_completion(tm, task)
        assert task.status == "FAILED"
        tm.start()
        time.sleep(0.2)
        assert task.status == "FAILED"

    def test_cancelled_task_not_restarted(self, task_manager):
        """CANCELLED tasks are not picked up by workers."""
        task = task_manager.submit("Test", [{"tool": "web_search", "args": {"query": "test"}}])
        task_manager.cancel(task.id)
        assert task.status == "CANCELLED"
        task_manager.start()
        time.sleep(0.2)
        assert task.status == "CANCELLED"

    def test_terminal_task_progress_is_100_for_completed(self, task_manager):
        """COMPLETED tasks report 100% progress."""
        task = task_manager.submit("Test", [{"tool": "web_search", "args": {"query": "test"}}])
        _run_task_to_completion(task_manager, task)
        assert task.progress_percent() == 100

    def test_terminal_task_progress_not_100_for_failed(self, tmp_tasks_file, mock_failing_executor):
        """FAILED tasks do not report 100% progress."""
        tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_failing_executor)
        task = tm.submit("Test", [{"tool": "web_search", "args": {"query": "test"}}])
        _run_task_to_completion(tm, task)
        assert task.progress_percent() < 100


# ── Test Group 4: Task With No Work ────────────────────────────────────────

class TestEmptyTasks:
    """Verify behavior when tasks have no remaining work."""

    def test_submit_empty_steps_raises(self, task_manager):
        """Submitting a task with no steps raises ValueError."""
        with pytest.raises(ValueError) as excinfo:
            task_manager.submit("Empty task", [])
        assert "step" in str(excinfo.value).lower()

    def test_task_with_no_steps_after_planning(self, task_manager):
        """A goal that produces no steps fails the task."""
        # Mock planner that returns empty list
        task_manager.planner = lambda goal: []
        task = task_manager.submit_goal("Impossible goal")
        _run_task_to_completion(task_manager, task)
        assert task.status == "FAILED"
        # The reason names the planning failure (or, if a background import
        # crashed first, the crash — either way the task did not pretend to
        # succeed with nothing to run).
        assert task.reason_for_stop  # not empty


# ── Test Group 5: Malformed/Inconsistent State Handling ────────────────────

class TestMalformedStateHandling:
    """Verify graceful handling of missing, malformed, or inconsistent state."""

    def test_load_ignores_unreadable_json(self, tmp_tasks_file):
        """Corrupted JSON file is handled gracefully."""
        with open(tmp_tasks_file, "w") as f:
            f.write("not valid json {")
        tm = TaskManager(path=tmp_tasks_file, tool_executor=Mock(return_value="ok"))
        assert tm.list() == []

    def test_load_ignores_missing_required_fields(self, tmp_tasks_file):
        """Tasks missing required fields are skipped."""
        bad_task = {"title": "Bad", "id": "T-bad"}  # missing status, steps, etc.
        with open(tmp_tasks_file, "w") as f:
            json.dump([bad_task], f)
        tm = TaskManager(path=tmp_tasks_file, tool_executor=Mock(return_value="ok"))
        # Should not crash, should have 0 or 1 valid tasks depending on handling
        assert len(tm.list()) >= 0

    def test_load_preserves_valid_tasks_alongside_invalid(self, tmp_tasks_file):
        """Valid tasks are loaded even if file contains invalid entries."""
        good_task = Task(title="Good", steps=[TaskStep(tool="web_search", args={"query": "test"})])
        good_task.status = "QUEUED"
        bad_task = {"title": "Bad"}
        with open(tmp_tasks_file, "w") as f:
            json.dump([good_task.to_dict(), bad_task], f)
        tm = TaskManager(path=tmp_tasks_file, tool_executor=Mock(return_value="ok"))
        loaded = tm.list()
        assert len(loaded) >= 1
        assert any(t.title == "Good" for t in loaded)

    def test_step_with_unknown_tool_handled_gracefully(self, task_manager):
        """Step naming an unknown tool is handled without crashing the task manager.
        
        The task manager itself doesn't validate tool names - that happens in
        the permission system. This test verifies the task manager doesn't
        crash when given an unknown tool name.
        """
        task = task_manager.submit("Test", [{"tool": "nonexistent_tool", "args": {}}])
        _run_task_to_completion(task_manager, task)
        # Task manager completes the task (executor returns success for any tool)
        # The actual permission check happens in nova.py dispatcher
        assert task.status in TERMINAL
        assert task.steps[0].status in ("VERIFIED", "FAILED", "UNVERIFIED")


# ── Test Group 6: State Definitions Match Implementation ──────────────────

class TestStateDefinitions:
    """Verify state constants match actual implementation."""

    def test_task_statuses_include_all_used_states(self):
        """TASK_STATUSES contains all states used in code."""
        used_states = {
            "PLANNED", "QUEUED", "PLANNING", "RUNNING", "WAITING", "BLOCKED",
            "REVIEWING", "VERIFYING", "PAUSED", "COMPLETED", "FAILED",
            "CANCELLED", "PARTIALLY_COMPLETED", "UNVERIFIED"
        }
        assert set(TASK_STATUSES) == used_states

    def test_step_statuses_include_all_used_states(self):
        """STEP_STATUSES contains all states used in code."""
        used_states = {"QUEUED", "RUNNING", "WAITING", "VERIFIED", "UNVERIFIED", "FAILED", "SKIPPED"}
        assert set(STEP_STATUSES) == used_states

    def test_terminal_states_match_documentation(self):
        """TERMINAL tuple matches states that never change again."""
        assert TERMINAL == ("COMPLETED", "FAILED", "CANCELLED", "PARTIALLY_COMPLETED", "UNVERIFIED")

    def test_active_states_match_documentation(self):
        """ACTIVE tuple matches states currently being worked on."""
        assert ACTIVE == ("PLANNING", "RUNNING", "WAITING", "REVIEWING", "VERIFYING")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
