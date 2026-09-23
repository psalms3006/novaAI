"""task_manager.py has called itself a "Concurrent Task Manager" in its own
module docstring since it was written. The implementation was a single
dedicated worker thread pulling one QUEUED task at a time -- the opposite
of that, and exactly the complaint "NOVA handles tasks one at a time, not
several at once".

These prove two independent tasks (no shared dependency) actually run at
the same time now, not just that they both eventually finish, and that
the "still working" signal stays true for as long as *any* task is still
running, not just the one a given worker happened to be watching.
"""
from __future__ import annotations

import threading
import time

import pytest

from task_manager import TaskManager


def _manager(tmp_path, executor, max_concurrent=3):
    return TaskManager(path=str(tmp_path / "tasks.json"), tool_executor=executor,
                       max_concurrent=max_concurrent)


def _wait_for(predicate, timeout=5.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_two_independent_tasks_run_with_overlapping_execution_windows(tmp_path):
    """The real test of concurrency: not that both finish, but that their
    RUNNING windows overlap in time -- something a single worker pulling
    one task at a time can never produce, however fast it is."""
    windows = {}
    lock = threading.Lock()

    def slow_executor(tool, args, meta):
        label = args.get("label", "?")
        start = time.time()
        time.sleep(0.3)
        end = time.time()
        with lock:
            windows[label] = (start, end)
        return "ok"

    tm = _manager(tmp_path, slow_executor, max_concurrent=3)
    task_a = tm.submit("Task A", [{"tool": "web_search", "args": {"label": "a"}}])
    task_b = tm.submit("Task B", [{"tool": "web_search", "args": {"label": "b"}}])

    tm.start()
    try:
        ok = _wait_for(lambda: task_a.status in
                       ("COMPLETED", "FAILED", "UNVERIFIED", "PARTIALLY_COMPLETED")
                       and task_b.status in
                       ("COMPLETED", "FAILED", "UNVERIFIED", "PARTIALLY_COMPLETED"))
        assert ok, "both tasks never reached a terminal status"
    finally:
        tm.stop()

    assert "a" in windows and "b" in windows
    a_start, a_end = windows["a"]
    b_start, b_end = windows["b"]
    overlap = min(a_end, b_end) - max(a_start, b_start)
    assert overlap > 0, (
        f"task windows did not overlap (a: {a_start:.3f}-{a_end:.3f}, "
        f"b: {b_start:.3f}-{b_end:.3f}) -- tasks ran one at a time, not "
        f"concurrently"
    )


def test_a_dependent_task_still_waits_for_its_dependency(tmp_path):
    """Concurrency must not mean "run everything regardless of
    dependencies" -- a task naming another as a dependency still has to
    wait for it, exactly as before."""
    order = []
    lock = threading.Lock()

    def executor(tool, args, meta):
        with lock:
            order.append(args.get("label"))
        # Must match _verify_step's success markers ("results:") or the
        # step verifies UNKNOWN and the task never reaches COMPLETED --
        # which would leave the dependent task waiting forever and prove
        # nothing about ordering.
        return "results: ok"

    tm = _manager(tmp_path, executor, max_concurrent=3)
    task_a = tm.submit("First", [{"tool": "web_search", "args": {"label": "first"}}])
    task_b = tm.submit("Second", [{"tool": "web_search", "args": {"label": "second"}}],
                       dependencies=[task_a.id])

    tm.start()
    try:
        ok = _wait_for(lambda: task_b.status in
                       ("COMPLETED", "FAILED", "UNVERIFIED", "PARTIALLY_COMPLETED"))
        assert ok, "the dependent task never finished"
    finally:
        tm.stop()

    assert order == ["first", "second"], (
        f"a dependent task ran before its dependency: {order}"
    )


def test_the_busy_signal_stays_true_until_every_task_is_done(tmp_path):
    """With one worker, "no more queued work" and "nothing is running"
    were the same question. With a pool they are not: this pins that the
    busy signal is not cleared while a sibling worker is still running a
    different task."""
    release = threading.Event()
    started = threading.Event()

    def executor(tool, args, meta):
        label = args.get("label")
        if label == "slow":
            started.set()
            release.wait(timeout=5.0)
        return "ok"

    busy_history = []
    tm = _manager(tmp_path, executor, max_concurrent=3)
    tm.set_on_activity(lambda busy: busy_history.append(busy))

    slow = tm.submit("Slow", [{"tool": "web_search", "args": {"label": "slow"}}])
    fast = tm.submit("Fast", [{"tool": "web_search", "args": {"label": "fast"}}])

    tm.start()
    try:
        assert started.wait(timeout=5.0), "the slow task never started"
        assert _wait_for(lambda: fast.status in
                         ("COMPLETED", "FAILED", "UNVERIFIED", "PARTIALLY_COMPLETED")), (
            "the fast task never finished"
        )
        # The fast task is done, but the slow one is still running --
        # activity must still read busy.
        time.sleep(0.1)
        assert busy_history and busy_history[-1] is True, (
            "activity was reported not-busy while a task was still running"
        )
        release.set()
        assert _wait_for(lambda: slow.status in
                         ("COMPLETED", "FAILED", "UNVERIFIED", "PARTIALLY_COMPLETED"))
        assert _wait_for(lambda: busy_history[-1] is False), (
            "activity never returned to not-busy once every task finished"
        )
    finally:
        release.set()
        tm.stop()
