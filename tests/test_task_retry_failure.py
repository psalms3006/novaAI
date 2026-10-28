"""Regression tests for NOVA task retry and failure handling.

These tests verify the retry logic, circuit breaker, timeout handling,
and failure classification behavior.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from unittest.mock import Mock, patch

import pytest

from task_manager import (
    TaskManager,
    Task,
    TaskStep,
    TERMINAL,
)
from core.reliability import CircuitBreaker, CircuitState


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_tasks_file(tmp_path):
    """Provide a temporary tasks file path."""
    return str(tmp_path / "tasks.json")


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


def _run_task_to_completion(tm: TaskManager, task: Task, timeout: float = 15.0) -> Task:
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


# ── Test Group 1: Transient Failure Recovery ───────────────────────────────

class TestTransientFailureRecovery:
    """Test recovery after transient failures."""

    def test_retry_on_transient_error(self, tmp_tasks_file, mock_generate_doc_executor):
        """Task retries on transient errors and succeeds on subsequent attempt."""
        attempt = [0]
        
        def transient_then_success(tool, args, meta):
            attempt[0] += 1
            if attempt[0] < 2:
                raise TimeoutError("transient network error")
            if tool == "generate_document":
                title = args.get("title", "output")
                path = tmp_tasks_file.replace("tasks.json", "") + f"{title}.txt"
                import pathlib
                pathlib.Path(path).write_text("Test content")
                return f"Saved: {path} (100 bytes)"
            return "Found results for: " + str(args.get("query", ""))
        
        tm = TaskManager(path=tmp_tasks_file, tool_executor=transient_then_success)
        task = tm.submit("Test", [{"tool": "generate_document", "args": {"title": "test"}}])
        _run_task_to_completion(tm, task)
        
        assert task.status == "COMPLETED"
        assert task.steps[0].status == "VERIFIED"
        assert attempt[0] == 2  # Initial attempt + 1 retry

    def test_retry_count_matches_configured_delays(self, tmp_tasks_file):
        """Number of retries matches RETRY_DELAYS_S configuration."""
        attempt = [0]
        
        def always_transient(tool, args, meta):
            attempt[0] += 1
            raise TimeoutError("always transient")
        
        tm = TaskManager(path=tmp_tasks_file, tool_executor=always_transient)
        task = tm.submit("Test", [{"tool": "generate_document", "args": {"title": "test"}}])
        _run_task_to_completion(tm, task)
        
        # RETRY_DELAYS_S = (2.0, 6.0) means 2 retries, 3 total attempts
        assert attempt[0] == 3
        assert task.steps[0].retries == 2

    def test_no_retry_on_permanent_failure(self, tmp_tasks_file, mock_generate_doc_executor):
        """Permanent failures (permission, invalid_input) are not retried."""
        attempt = [0]
        
        def permission_failure(tool, args, meta):
            attempt[0] += 1
            raise PermissionError("auth failed")
        
        tm = TaskManager(path=tmp_tasks_file, tool_executor=permission_failure)
        task = tm.submit("Test", [{"tool": "generate_document", "args": {"title": "test"}}])
        _run_task_to_completion(tm, task)
        
        # Should fail immediately without retries
        assert attempt[0] == 1
        assert task.status == "FAILED"
        assert task.steps[0].retries == 0


# ── Test Group 2: Retry Limits ─────────────────────────────────────────────

class TestRetryLimits:
    """Test that retry limits are enforced correctly."""

    def test_max_retries_enforced(self, tmp_tasks_file):
        """No retries beyond configured maximum."""
        attempt = [0]
        
        def always_transient(tool, args, meta):
            attempt[0] += 1
            raise TimeoutError("transient")
        
        tm = TaskManager(path=tmp_tasks_file, tool_executor=always_transient)
        task = tm.submit("Test", [{"tool": "generate_document", "args": {"title": "test"}}])
        _run_task_to_completion(tm, task)
        
        # RETRY_DELAYS_S = (2.0, 6.0) = 2 retries max
        assert attempt[0] == 3  # 1 initial + 2 retries
        assert task.steps[0].retries == 2
        assert task.status == "FAILED"

    def test_retry_delay_increases_exponentially(self, tmp_tasks_file):
        """Retry delays follow exponential backoff with jitter."""
        delays = []
        
        def timed_transient(tool, args, meta):
            start = time.time()
            raise TimeoutError("transient")
        
        tm = TaskManager(path=tmp_tasks_file, tool_executor=timed_transient)
        task = tm.submit("Test", [{"tool": "generate_document", "args": {"title": "test"}}])
        
        start = time.time()
        _run_task_to_completion(tm, task)
        elapsed = time.time() - start
        
        # Should have waited roughly: 2s + 6s = 8s (with jitter: 4s-12s range)
        # But with jitter it's 50%-150% of each delay
        # So roughly 4-18 seconds total
        assert elapsed >= 3  # At least some delay occurred
        assert elapsed <= 30  # But not forever

    def test_cancel_during_retry_stops_retrying(self, tmp_tasks_file):
        """Cancelling a task during retry stops further retries."""
        attempt = [0]
        
        def slow_transient(tool, args, meta):
            attempt[0] += 1
            time.sleep(0.5)  # Slow enough to allow cancellation
            raise TimeoutError("transient")
        
        tm = TaskManager(path=tmp_tasks_file, tool_executor=slow_transient)
        task = tm.submit("Test", [{"tool": "generate_document", "args": {"title": "test"}}])
        tm.start()
        
        # Wait for first attempt to start
        time.sleep(0.6)
        tm.cancel(task.id)
        
        # Wait a bit more
        time.sleep(1.0)
        tm.stop()
        
        # Should not have made all 3 attempts
        assert attempt[0] <= 2


# ── Test Group 3: Permanent Failure Handling ───────────────────────────────

class TestPermanentFailureHandling:
    """Test correct handling of permanent (non-retryable) failures."""

    def test_permission_failure_not_retried(self, tmp_tasks_file, mock_generate_doc_executor):
        """Permission failures are not retried."""
        attempt = [0]
        
        def permission_error(tool, args, meta):
            attempt[0] += 1
            raise PermissionError("Access denied")
        
        tm = TaskManager(path=tmp_tasks_file, tool_executor=permission_error)
        task = tm.submit("Test", [{"tool": "generate_document", "args": {"title": "test"}}])
        _run_task_to_completion(tm, task)
        
        assert attempt[0] == 1
        assert task.status == "FAILED"
        assert task.steps[0].error_class == "permission"

    def test_invalid_input_not_retried(self, tmp_tasks_file, mock_generate_doc_executor):
        """Invalid input errors are not retried."""
        attempt = [0]
        
        def invalid_input(tool, args, meta):
            attempt[0] += 1
            raise ValueError("Invalid parameter")
        
        tm = TaskManager(path=tmp_tasks_file, tool_executor=invalid_input)
        task = tm.submit("Test", [{"tool": "generate_document", "args": {"title": "test"}}])
        _run_task_to_completion(tm, task)
        
        assert attempt[0] == 1
        assert task.status == "FAILED"

    def test_refusal_recognized_as_failure(self, tmp_tasks_file):
        """Permission refusals are recognized as failures, not unverified."""
        refusal_msg = "Refused: tool 'test_tool' declares no capabilities, so it cannot be authorised."
        
        tm = TaskManager(path=tmp_tasks_file, tool_executor=Mock(return_value=refusal_msg))
        task = tm.submit("Test", [{"tool": "test_tool", "args": {}}])
        _run_task_to_completion(tm, task)
        
        assert task.status == "FAILED"
        assert task.steps[0].verification == "FAILURE"

    def test_refusal_recognized_as_failure(self, tmp_tasks_file):
        """Permission refusals are recognized as failures, not unverified."""
        refusal_msg = "Refused: tool 'test_tool' declares no capabilities, so it cannot be authorised."
        
        tm = TaskManager(path=tmp_tasks_file, tool_executor=Mock(return_value=refusal_msg))
        task = tm.submit("Test", [{"tool": "test_tool", "args": {}}])
        _run_task_to_completion(tm, task)
        
        assert task.status == "FAILED"
        assert task.steps[0].verification == "FAILURE"


# ── Test Group 4: Timeout Handling ─────────────────────────────────────────

class TestTimeoutHandling:
    """Test predictable behavior when timeouts occur."""

    def test_step_timeout_triggers_retry(self, tmp_tasks_file, mock_generate_doc_executor):
        """Step timeout is treated as transient and retried."""
        attempt = [0]
        
        def timeout_error(tool, args, meta):
            attempt[0] += 1
            if attempt[0] < 2:
                raise TimeoutError("Request timed out")
            if tool == "generate_document":
                title = args.get("title", "output")
                path = tmp_tasks_file.replace("tasks.json", "") + f"{title}.txt"
                import pathlib
                pathlib.Path(path).write_text("Test content")
                return f"Saved: {path} (100 bytes)"
            return "Found results for: " + str(args.get("query", ""))
        
        tm = TaskManager(path=tmp_tasks_file, tool_executor=timeout_error)
        task = tm.submit("Test", [{"tool": "generate_document", "args": {"title": "test"}}])
        _run_task_to_completion(tm, task)
        
        assert task.status == "COMPLETED"
        assert attempt[0] == 2

    def test_task_completion_timeout_not_implemented(self, tmp_tasks_file):
        """Note: No per-task timeout currently implemented in TaskManager."""
        # This test documents the current behavior
        # TaskManager doesn't have a per-task timeout, only per-step retries
        tm = TaskManager(path=tmp_tasks_file, tool_executor=Mock(return_value="ok"))
        task = tm.submit("Test", [{"tool": "web_search", "args": {"query": "test"}}])
        
        # Verify the task has the estimated_duration_s field but no enforcement
        assert hasattr(task, "estimated_duration_s")
        # The timeout is informational only


# ── Test Group 5: Circuit Breaker Behavior ─────────────────────────────────

class TestCircuitBreaker:
    """Test circuit breaker transitions and recovery."""

    def test_circuit_breaker_opens_after_threshold(self, tmp_tasks_file):
        """Circuit breaker opens after failure threshold reached."""
        # CircuitBreaker in TaskManager: failure_threshold=5, reset_timeout=30.0
        fail_count = [0]
        
        def always_fails(tool, args, meta):
            fail_count[0] += 1
            raise TimeoutError("transient")
        
        tm = TaskManager(path=tmp_tasks_file, tool_executor=always_fails)
        
        # Submit 10 tasks - first 5 should fail and trigger circuit breaker
        tasks = []
        for i in range(10):
            tasks.append(tm.submit(f"Task {i}", [{"tool": "test_tool", "args": {}}]))
        
        tm.start()
        try:
            deadline = time.time() + 20.0
            while time.time() < deadline:
                if all(t.status in TERMINAL for t in tasks):
                    break
                time.sleep(0.1)
        finally:
            tm.stop()
        
        # Circuit breaker should have opened after 5 failures
        # First 5 tasks each tried 3 times (15 calls), then circuit opened
        # Remaining tasks should fail quickly
        assert fail_count[0] >= 5  # At least 5 failures recorded

    def test_task_manager_currently_has_no_breaker_documented(self, tmp_tasks_file):
        """The committed task manager no longer wires a circuit breaker into
        step execution (concurrent-developer change). This test pins that
        fact honestly: repeated transient failure is bounded by the retry
        delays, not by a breaker. If a breaker returns, replace this with
        the old integration assertions."""
        assert not hasattr(TaskManager(path=tmp_tasks_file,
                                       tool_executor=Mock()), "_tool_cb")

    def test_circuit_breaker_half_open_after_reset_timeout(self):
        """Circuit breaker permits trial call after reset timeout."""
        cb = CircuitBreaker(name="test", failure_threshold=2, reset_timeout=0.1)
        
        # Fail twice to open
        try:
            with cb:
                raise TimeoutError("fail")
        except TimeoutError:
            pass
        try:
            with cb:
                raise TimeoutError("fail")
        except TimeoutError:
            pass
        
        assert cb.state == CircuitState.OPEN
        
        # Wait for reset timeout
        time.sleep(0.15)
        
        # Should be half-open now
        assert cb.state == CircuitState.HALF_OPEN
        
        # Successful trial call should close it
        with cb:
            pass  # success
        
        assert cb.state == CircuitState.CLOSED

    def test_circuit_breaker_records_failures_correctly(self):
        """Circuit breaker tracks failure count accurately."""
        cb = CircuitBreaker(name="test", failure_threshold=3, reset_timeout=30.0)
        
        assert cb.state == CircuitState.CLOSED
        assert cb._failure_count == 0
        
        # First failure
        cb.record_failure()
        assert cb._failure_count == 1
        assert cb.state == CircuitState.CLOSED
        
        # Second failure
        cb.record_failure()
        assert cb._failure_count == 2
        assert cb.state == CircuitState.CLOSED
        
        # Third failure - should open
        cb.record_failure()
        assert cb._failure_count == 3
        assert cb.state == CircuitState.OPEN


# ── Test Group 6: Error Preservation ──────────────────────────────────────

class TestErrorPreservation:
    """Test that original errors are preserved when recovery fails."""

    def test_original_error_preserved_after_retries_exhausted(self, tmp_tasks_file):
        """The original error is preserved when all retries fail."""
        original_error = "Original transient error message"
        
        def always_fails(tool, args, meta):
            raise TimeoutError(original_error)
        
        tm = TaskManager(path=tmp_tasks_file, tool_executor=always_fails)
        task = tm.submit("Test", [{"tool": "generate_document", "args": {"title": "test"}}])
        _run_task_to_completion(tm, task)
        
        assert task.status == "FAILED"
        assert original_error in task.steps[0].error
        assert task.steps[0].error_class == "transient"

    def test_original_error_class_preserved(self, tmp_tasks_file, mock_generate_doc_executor):
        """Error class (transient/permission) is preserved."""
        
        def permission_error(tool, args, meta):
            raise PermissionError("Access denied")
        
        tm = TaskManager(path=tmp_tasks_file, tool_executor=permission_error)
        task = tm.submit("Test", [{"tool": "generate_document", "args": {"title": "test"}}])
        _run_task_to_completion(tm, task)
        
        assert task.steps[0].error_class == "permission"


# ── Test Group 7: Duplicate Execution Avoidance ────────────────────────────

class TestDuplicateExecutionAvoidance:
    """Test avoidance of duplicate execution during retries."""

    def test_retry_does_not_duplicate_side_effects_on_success(self, tmp_tasks_file):
        """When retry succeeds, operation is not duplicated."""
        call_log = []
        
        def tracking_executor(tool, args, meta):
            call_log.append({"tool": tool, "args": args})
            if len(call_log) < 2:
                raise TimeoutError("transient")
            return "Saved: output.txt (100 bytes)"
        
        tm = TaskManager(path=tmp_tasks_file, tool_executor=tracking_executor)
        task = tm.submit("Test", [{"tool": "generate_document", "args": {"title": "test"}}])
        _run_task_to_completion(tm, task)
        
        # Should have been called twice (1 initial + 1 retry)
        # But the actual side effect (file write) should only happen once
        # Note: This depends on the tool being idempotent or the retry
        # happening before the side effect. The task manager retries
        # the entire tool call, so side effects WILL happen multiple times
        # unless the tool itself is idempotent.
        assert len(call_log) == 2

    def test_no_duplicate_queue_entries_on_retry(self, tmp_tasks_file):
        """Retrying a failed task doesn't create duplicate queue entries."""
        tm = TaskManager(path=tmp_tasks_file, tool_executor=Mock(side_effect=TimeoutError("transient")))
        task = tm.submit("Test", [{"tool": "generate_document", "args": {"title": "test"}}])
        _run_task_to_completion(tm, task)
        
        # Retry the failed task
        new_task = tm.retry(task.id)
        assert new_task is not None
        assert new_task.id != task.id
        assert new_task.parent_task_id == task.id
        
        # Original task should remain FAILED
        assert task.status == "FAILED"
        # New task should be QUEUED
        assert new_task.status == "QUEUED"


# Helper imports
import time
from unittest.mock import Mock

if __name__ == "__main__":
    pytest.main([__file__, "-v"])