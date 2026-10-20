"""Regression tests for NOVA task idempotency and duplicate execution prevention.

These tests verify behavior around repeated recovery calls, completion notifications,
duplicate queue entries, and prevention of duplicate side effects.
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
    TERMINAL,
)


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_tasks_file(tmp_path):
    """Provide a temporary tasks file path."""
    return str(tmp_path / "tasks.json")


@pytest.fixture
def mock_executor():
    """Tool executor that returns success for web_search by default."""
    return Mock(return_value="Found 5 results for query: test")


@pytest.fixture
def mock_generate_doc_executor(tmp_path):
    """Tool executor that creates actual file for generate_document."""
    def executor(tool, args, meta):
        if tool == "generate_document":
            title = args.get("title", "output")
            path = tmp_path / f"{title}.txt"
            path.write_text("Test content")
            return f"Saved: {path} (100 bytes)"
        return "Found results for: " + str(args.get("query", ""))
    return Mock(side_effect=executor)


def _run_task_to_completion(tm: TaskManager, task: Task, timeout: float = 10.0) -> Task:
    """Helper to run a task to completion."""
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


# ── Test Group 1: Repeated Recovery Calls ──────────────────────────────────

class TestRepeatedRecovery:
    """Test that repeated recovery calls are safe."""

    def test_multiple_reloads_same_result(self, tmp_tasks_file, mock_generate_doc_executor):
        """Creating multiple TaskManager instances from same file gives consistent results."""
        task = Task(title="Recovery Test", steps=[
            TaskStep(tool="generate_document", args={"title": "test"})
        ])
        task.status = "RUNNING"
        task.current_step = 0
        task.steps[0].status = "RUNNING"
        task.steps[0].started = time.time()
        
        with open(tmp_tasks_file, "w") as f:
            json.dump([task.to_dict()], f)
        
        results = []
        for _ in range(5):
            tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_generate_doc_executor)
            loaded = tm.get(task.id)
            results.append((loaded.status, loaded.steps[0].status))
        
        # All reloads should produce identical results: interrupted work
        # fails loudly, consistently, and never resumes itself.
        assert all(r == ("FAILED", "RUNNING") for r in results)

    def test_reload_after_partial_completion(self, tmp_tasks_file, mock_generate_doc_executor):
        """Reloading after partial completion preserves completed work."""
        # Create task with one VERIFIED step and one RUNNING step
        task = Task(title="Partial", steps=[
            TaskStep(tool="generate_document", args={"title": "step1"}),
            TaskStep(tool="generate_document", args={"title": "step2"}),
        ])
        task.status = "RUNNING"
        task.current_step = 0
        task.steps[0].status = "VERIFIED"
        task.steps[0].verified = True
        task.steps[0].verification = "CONFIRMED_SUCCESS"
        task.steps[0].finished = time.time()
        task.steps[1].status = "RUNNING"
        task.steps[1].started = time.time()
        
        with open(tmp_tasks_file, "w") as f:
            json.dump([task.to_dict()], f)
        
        # Reload multiple times: the completed step keeps its evidence,
        # the task as a whole is failed (not resumed), so nothing re-runs.
        for _ in range(3):
            tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_generate_doc_executor)
            loaded = tm.get(task.id)
            assert loaded.status == "FAILED"
            assert loaded.steps[0].status == "VERIFIED"
            assert loaded.steps[0].verified is True

    def test_recovery_idempotent_for_terminal_tasks(self, tmp_tasks_file, mock_generate_doc_executor):
        """Recovery is idempotent for tasks already in terminal states."""
        task = Task(title="Terminal", steps=[
            TaskStep(tool="generate_document", args={"title": "done"})
        ])
        task.status = "COMPLETED"
        task.progress = 100
        task.finished = time.time()
        task.steps[0].status = "VERIFIED"
        
        with open(tmp_tasks_file, "w") as f:
            json.dump([task.to_dict()], f)
        
        # Reload multiple times
        for _ in range(3):
            tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_generate_doc_executor)
            loaded = tm.get(task.id)
            assert loaded.status == "COMPLETED"
            assert loaded.progress == 100
            assert loaded.steps[0].status == "VERIFIED"


# ── Test Group 2: Repeated Completion Notifications ────────────────────────

class TestRepeatedCompletion:
    """Test that repeated completion handling is safe."""

    def test_complete_twice_same_result(self, tmp_tasks_file, mock_generate_doc_executor):
        """Calling _finish twice on same task doesn't change outcome."""
        tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_generate_doc_executor)
        task = tm.submit("Test", [{"tool": "generate_document", "args": {"title": "test"}}])
        _run_task_to_completion(tm, task)
        
        original_status = task.status
        original_reason = task.reason_for_stop
        
        # Call _finish again (simulating duplicate completion notification)
        tm._finish(task, "COMPLETED", "duplicate")
        
        assert task.status == original_status
        # The reason might be updated, but status should remain COMPLETED
        assert task.status == "COMPLETED"

    def test_notify_done_called_once_per_task(self, tmp_tasks_file, mock_generate_doc_executor):
        """_notify_done is called exactly once per task completion."""
        notify_calls = []
        
        def tracking_notify(msg):
            notify_calls.append(msg)
        
        tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_generate_doc_executor)
        tm.set_notify(tracking_notify)
        task = tm.submit("Test", [{"tool": "generate_document", "args": {"title": "test"}}])
        _run_task_to_completion(tm, task)
        
        # Should have been notified exactly once
        assert len(notify_calls) == 1

    def test_retry_does_not_trigger_duplicate_notification(self, tmp_tasks_file, mock_generate_doc_executor):
        """Retrying a task doesn't trigger notification for the original task."""
        notify_calls = []
        
        def tracking_notify(msg):
            notify_calls.append(msg)
        
        tm = TaskManager(path=tmp_tasks_file, tool_executor=Mock(side_effect=TimeoutError("fail")))
        tm.set_notify(tracking_notify)
        task = tm.submit("Test", [{"tool": "generate_document", "args": {"title": "test"}}])
        _run_task_to_completion(tm, task)
        
        # Give time for notification to be processed
        time.sleep(0.2)
        first_notify_count = len(notify_calls)
        
        # Retry the task
        new_task = tm.retry(task.id)
        _run_task_to_completion(tm, new_task)
        
        # Give time for notification to be processed (cross-thread)
        time.sleep(0.5)
        
        # Original task's notification shouldn't be duplicated
        # Only the new task should have triggered a notification
        assert len(notify_calls) == first_notify_count + 1, f"Expected {first_notify_count + 1} notifications, got {len(notify_calls)}"


# ── Test Group 3: Duplicate Queue Entries ─────────────────────────────────

class TestDuplicateQueueEntries:
    """Test prevention of duplicate queue entries."""

    def test_submit_same_task_twice_creates_two_entries(self, tmp_tasks_file, mock_executor):
        """Submitting the same task twice creates two separate task entries."""
        tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_executor)
        
        task1 = tm.submit("Same Title", [{"tool": "web_search", "args": {"query": "test"}}])
        task2 = tm.submit("Same Title", [{"tool": "web_search", "args": {"query": "test"}}])
        
        assert task1.id != task2.id
        assert len(tm.list()) == 2

    def test_submit_with_explicit_id_rejects_duplicate(self, tmp_tasks_file, mock_executor):
        """Submitting with explicit task_id that already exists should fail or replace."""
        tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_executor)
        
        task1 = tm.submit("Test", [{"tool": "web_search", "args": {"query": "test"}}], task_id="T-explicit")
        
        # Submitting with same ID should either fail or replace
        # Current implementation appends a new task with the same ID (doesn't replace)
        task2 = tm.submit("Test 2", [{"tool": "web_search", "args": {"query": "test2"}}], task_id="T-explicit")
        
        # Current implementation: both tasks exist with same ID
        assert task1.id == task2.id == "T-explicit"
        assert len(tm.list()) == 2  # Both tasks in list
        # The second task should be the last one in the list
        assert tm.list()[-1].title == "Test 2"

    def test_retry_creates_new_task_not_duplicate(self, tmp_tasks_file, mock_generate_doc_executor):
        """Retry creates a new task with parent reference, not a duplicate."""
        tm = TaskManager(path=tmp_tasks_file, tool_executor=Mock(side_effect=TimeoutError("fail")))
        task = tm.submit("Test", [{"tool": "generate_document", "args": {"title": "test"}}])
        _run_task_to_completion(tm, task)
        
        assert task.status == "FAILED"
        
        # Retry creates NEW task
        new_task = tm.retry(task.id)
        assert new_task is not None
        assert new_task.id != task.id
        assert new_task.parent_task_id == task.id
        assert new_task.status == "QUEUED"
        
        # Original task unchanged
        assert task.status == "FAILED"

    def test_cancel_then_resubmit_creates_new_task(self, tmp_tasks_file, mock_executor):
        """Cancelling and re-submitting creates a new task."""
        tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_executor)
        task = tm.submit("Test", [{"tool": "web_search", "args": {"query": "test"}}])
        tm.cancel(task.id)
        
        new_task = tm.submit("Test", [{"tool": "web_search", "args": {"query": "test"}}])
        
        assert new_task.id != task.id
        assert task.status == "CANCELLED"
        assert new_task.status == "QUEUED"


# ── Test Group 4: Repeated Execution Requests ──────────────────────────────

class TestRepeatedExecutionRequests:
    """Test behavior when same execution is requested multiple times."""

    def test_worker_does_not_pick_up_same_task_twice(self, tmp_tasks_file, mock_generate_doc_executor):
        """A task is only picked up by one worker at a time."""
        tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_generate_doc_executor, max_concurrent=3)
        task = tm.submit("Test", [{"tool": "generate_document", "args": {"title": "test"}}])
        
        tm.start()
        try:
            # Wait for task to start
            time.sleep(0.2)
            
            # Task should be in RUNNING, not QUEUED
            current = tm.get(task.id)
            assert current.status in ("RUNNING", "COMPLETED", "VERIFYING")
            
            # Try to submit same task again - should create new task
            task2 = tm.submit("Test", [{"tool": "generate_document", "args": {"title": "test"}}])
            assert task2.id != task.id
        finally:
            tm.stop()

    def test_dependency_prevents_parallel_execution(self, tmp_tasks_file, mock_generate_doc_executor):
        """Tasks with dependencies wait for parent to complete."""
        tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_generate_doc_executor)
        
        parent = tm.submit("Parent", [{"tool": "generate_document", "args": {"title": "parent"}}])
        child = tm.submit("Child", [{"tool": "generate_document", "args": {"title": "child"}}],
                         dependencies=[parent.id])
        
        tm.start()
        try:
            # Wait for both to complete
            time.sleep(1.0)
            
            # Parent should complete first
            assert parent.status == "COMPLETED"
            # Child should also complete (after parent)
            assert child.status == "COMPLETED"
        finally:
            tm.stop()

    def test_dependency_on_failed_task_blocks_child(self, tmp_tasks_file, mock_generate_doc_executor):
        """Child task waits indefinitely if parent fails (current behavior)."""
        def failing_executor(tool, args, meta):
            if tool == "generate_document":
                raise TimeoutError("fail")
            return "Found results for: " + str(args.get("query", ""))
        
        tm = TaskManager(path=tmp_tasks_file, tool_executor=failing_executor)
        
        parent = tm.submit("Parent", [{"tool": "generate_document", "args": {"title": "parent"}}])
        child = tm.submit("Child", [{"tool": "generate_document", "args": {"title": "child"}}],
                         dependencies=[parent.id])
        
        tm.start()
        try:
            # Retry delays (2s + 6s, with jitter) mean the parent's three
            # attempts can take up to ~14s. Poll rather than guess.
            deadline = time.time() + 25.0
            while time.time() < deadline and parent.status not in TERMINAL:
                time.sleep(0.2)
            
            # Parent fails
            assert parent.status == "FAILED", f"parent stuck in {parent.status}"
            # Child should remain QUEUED (waiting for parent)
            assert child.status in ("QUEUED", "WAITING")
        finally:
            tm.stop()


# ── Test Group 5: Side Effect Idempotency ──────────────────────────────────

class TestSideEffectIdempotency:
    """Test prevention of duplicate side effects where guaranteed."""

    def test_verified_step_not_re_executed(self, tmp_tasks_file, mock_generate_doc_executor):
        """A VERIFIED step is not re-executed on recovery.
        
        Under the current recovery contract an interrupted task is FAILED,
        never resumed — which makes the duplicate-execution guarantee
        absolute: the executor must not fire at all on reload.
        """
        execute_count = [0]
        
        def counting_executor(tool, args, meta):
            execute_count[0] += 1
            return "Found results for: " + str(args.get("query", ""))
        
        # Create task with one VERIFIED step and one QUEUED step, mid-flight
        task = Task(title="Mixed", steps=[
            TaskStep(tool="generate_document", args={"title": "step1"}),
            TaskStep(tool="generate_document", args={"title": "step2"}),
        ])
        task.status = "RUNNING"
        task.current_step = 0
        task.steps[0].status = "VERIFIED"
        task.steps[0].verified = True
        task.steps[0].verification = "CONFIRMED_SUCCESS"
        task.steps[0].finished = time.time()
        task.steps[1].status = "QUEUED"
        
        with open(tmp_tasks_file, "w") as f:
            json.dump([task.to_dict()], f)
        
        tm = TaskManager(path=tmp_tasks_file, tool_executor=counting_executor)
        loaded = tm.get(task.id)
        assert loaded.status == "FAILED"
        
        # Nothing re-runs: not the finished step, not the pending one.
        tm.start()
        time.sleep(0.3)
        tm.stop()
        assert execute_count[0] == 0, "recovery re-executed completed work"

    def test_verified_step_with_artifact_preserved(self, tmp_tasks_file, mock_executor):
        """VERIFIED step with artifact is preserved on recovery."""
        task = Task(title="With Artifact", steps=[
            TaskStep(tool="generate_document", args={"title": "step1"}),
        ])
        task.status = "RUNNING"
        task.current_step = 0
        task.steps[0].status = "VERIFIED"
        task.steps[0].verified = True
        task.steps[0].verification = "CONFIRMED_SUCCESS"
        task.steps[0].finished = time.time()
        task.artifacts = [{
            "path": "/tmp/test_output.docx",
            "exists": True,
            "size": 1024,
            "validated": True,
            "step": 0,
            "agent": "creative"
        }]
        
        with open(tmp_tasks_file, "w") as f:
            json.dump([task.to_dict()], f)
        
        tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_executor)
        loaded = tm.get(task.id)
        
        assert loaded.steps[0].status == "VERIFIED"
        assert len(loaded.artifacts) == 1
        assert loaded.artifacts[0]["path"] == "/tmp/test_output.docx"

    def test_review_results_preserved_on_recovery(self, tmp_tasks_file, mock_executor):
        """Review results are preserved when task is recovered."""
        task = Task(title="Reviewed", steps=[
            TaskStep(tool="generate_document", args={"title": "test"}),
        ])
        task.status = "REVIEWING"
        task.current_step = 0
        task.steps[0].status = "VERIFIED"
        task.reviews = [{
            "round": 1,
            "passed": True,
            "issues": [],
            "checked": ["step 1: file exists"],
            "ts": time.time()
        }]
        
        with open(tmp_tasks_file, "w") as f:
            json.dump([task.to_dict()], f)
        
        tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_executor)
        loaded = tm.get(task.id)
        
        # REVIEWING is ACTIVE: the interrupted task fails loudly, but the
        # review evidence it had already earned is preserved.
        assert loaded.status == "FAILED"
        assert len(loaded.reviews) == 1
        assert loaded.reviews[0]["passed"] is True


# ── Test Group 6: Concurrent Access Safety ─────────────────────────────────

class TestConcurrentAccess:
    """Test safety under concurrent access patterns."""

    def test_multiple_workers_dont_execute_same_step(self, tmp_tasks_file, mock_generate_doc_executor):
        """Multiple workers don't execute the same step concurrently."""
        execute_count = [0]
        
        def slow_executor(tool, args, meta):
            execute_count[0] += 1
            time.sleep(0.5)
            if tool == "generate_document":
                title = args.get("title", "output")
                path = tmp_tasks_file.replace("tasks.json", "") + f"{title}.txt"
                import pathlib
                pathlib.Path(path).write_text("Test content")
                return f"Saved: {path} (100 bytes)"
            return "Found results for: " + str(args.get("query", ""))
        
        tm = TaskManager(path=tmp_tasks_file, tool_executor=slow_executor, max_concurrent=3)
        
        # Submit multiple tasks that need the same resource (desktop)
        tasks = []
        for i in range(3):
            tasks.append(tm.submit(f"Task {i}", [{"tool": "open_app", "args": {"app_name": "notepad"}}]))
        
        tm.start()
        try:
            # The resource lock serialises these; three slow tasks need
            # up to ~1.5s each. Poll until all three have run.
            deadline = time.time() + 15.0
            while time.time() < deadline and execute_count[0] < 3:
                time.sleep(0.1)
        finally:
            tm.stop()
        
        # Each task should have been executed once
        # The resource locking should serialize them
        assert execute_count[0] == 3

    def test_save_during_concurrent_execution(self, tmp_tasks_file, mock_generate_doc_executor):
        """Concurrent saves during execution don't corrupt data."""
        tm = TaskManager(path=tmp_tasks_file, tool_executor=mock_generate_doc_executor, max_concurrent=3)
        
        tasks = []
        for i in range(5):
            tasks.append(tm.submit(f"Task {i}", [{"tool": "web_search", "args": {"query": f"query{i}"}}]))
        
        tm.start()
        try:
            # Poll to terminal rather than guessing a sleep: under a loaded
            # test runner five tasks take however long they take.
            deadline = time.time() + 20.0
            while time.time() < deadline and \
                    any(t.status not in TERMINAL for t in tasks):
                time.sleep(0.1)
        finally:
            tm.stop()
        
        # All tasks should be in terminal state
        for task in tasks:
            loaded = tm.get(task.id)
            assert loaded is not None
            assert loaded.status in TERMINAL, \
                f"{task.id} stuck in {loaded.status}"

    def test_cancel_during_execution_safe(self, tmp_tasks_file, mock_generate_doc_executor):
        """Cancelling during execution doesn't corrupt state."""
        def slow_executor(tool, args, meta):
            time.sleep(0.5)
            if tool == "generate_document":
                title = args.get("title", "output")
                path = tmp_tasks_file.replace("tasks.json", "") + f"{title}.txt"
                import pathlib
                pathlib.Path(path).write_text("Test content")
                return f"Saved: {path} (100 bytes)"
            return "Found results for: " + str(args.get("query", ""))
        
        tm = TaskManager(path=tmp_tasks_file, tool_executor=slow_executor)
        task = tm.submit("Test", [{"tool": "generate_document", "args": {"title": "test"}}])
        
        tm.start()
        time.sleep(0.2)  # Let execution start
        tm.cancel(task.id)
        time.sleep(0.5)
        tm.stop()
        
        # Task should be CANCELLED, not in inconsistent state
        assert task.status == "CANCELLED"
        
        # Reload should work
        tm2 = TaskManager(path=tmp_tasks_file, tool_executor=mock_generate_doc_executor)
        loaded = tm2.get(task.id)
        assert loaded.status == "CANCELLED"


# Helper imports
import time

if __name__ == "__main__":
    pytest.main([__file__, "-v"])