"""The orb has to show the two things it currently cannot: work, and being cut off.

Two gaps, both of them cases where NOVA knows something and the surface does
not:

* **Background work is invisible.** Now that one spoken request can start a
  task that runs for a while, the orb sits at "listening" throughout. From the
  user's side that is indistinguishable from NOVA having ignored them.

* **Interruption is published and unhandled.** `LiveManager` emits an
  `interrupted` event when the user talks over NOVA, and no frontend handler
  exists for it, so the one moment the user actively took control looks the
  same as any other.

The canonical channel already exists -- `desk.bridge.publish_orb_state` -- so
neither of these needs a new transport, only for the signal to be connected to
it.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from task_manager import TaskManager

# The checks on the previous window (desk/static) that lived here moved to
# tests/test_desk_ui_behaviour.py when the interface became desk/ui.

REPO = Path(__file__).resolve().parents[1]


def _manager(tmp_path, executor=None, **kw):
    return TaskManager(
        path=str(tmp_path / "tasks.json"),
        tool_executor=executor or (lambda tool, args, meta: "Saved: out.txt"),
        **kw,
    )


def _drain(manager, task, timeout=10.0, settle=None):
    """Run the manager until the task finishes.

    `settle` is the activity list, when the caller cares about it. The task's
    status reaches its terminal value inside _run_task and the activity
    release fires afterwards in the worker's finally, so waiting on the status
    alone leaves a window where the work is done and the signal has not
    landed. That window is invisible in isolation and opens under the load of
    the full suite.
    """
    terminal = {"COMPLETED", "FAILED", "CANCELLED", "PARTIALLY_COMPLETED",
                "UNVERIFIED"}

    def _done():
        if task.status not in terminal:
            return False
        if settle is None:
            return True
        return bool(settle) and settle[-1] is False

    manager.start()
    try:
        deadline = time.time() + timeout
        while time.time() < deadline and not _done():
            time.sleep(0.02)
    finally:
        manager.stop()
    return task


def test_the_task_manager_reports_when_it_starts_and_stops_working(tmp_path):
    """Fails until there is any way to know work is happening."""
    seen = []
    manager = _manager(tmp_path)
    assert hasattr(manager, "set_on_activity"), (
        "nothing can observe that a background task is running"
    )
    manager.set_on_activity(lambda busy: seen.append(busy))

    task = manager.submit("Research something",
                          [{"tool": "web_search", "args": {"query": "x"}}])
    _drain(manager, task, settle=seen)

    assert seen, "the task ran and reported nothing"
    assert seen[0] is True, f"first signal should be 'busy': {seen}"
    assert seen[-1] is False, f"work finished without saying so: {seen}"


def test_the_activity_signal_settles_after_the_last_task(tmp_path):
    """Two tasks must not leave the orb stuck showing work."""
    seen = []
    manager = _manager(tmp_path)
    manager.set_on_activity(lambda busy: seen.append(busy))

    manager.submit("One", [{"tool": "web_search", "args": {"query": "a"}}])
    second = manager.submit("Two", [{"tool": "web_search", "args": {"query": "b"}}])
    manager.start()
    try:
        # Wait for the *signal*, not the task status.
        #
        # The status reaches COMPLETED inside _run_task, and the release fires
        # afterwards in the worker's finally. Waiting on the status therefore
        # has a window where the task is done and the signal has not landed —
        # which passed in isolation and failed under the load of the full
        # suite, the least useful kind of red.
        deadline = time.time() + 10
        while time.time() < deadline and not (seen and seen[-1] is False):
            time.sleep(0.02)
    finally:
        manager.stop()

    assert second.status == "COMPLETED", second.reason_for_stop
    assert seen[-1] is False, f"ended busy: {seen}"


def test_a_failing_observer_does_not_disturb_the_work(tmp_path):
    """This is called from the worker thread."""
    def explode(_busy):
        raise RuntimeError("the surface is gone")

    manager = _manager(tmp_path)
    manager.set_on_activity(explode)
    task = manager.submit("Still runs",
                          [{"tool": "web_search", "args": {"query": "x"}}])
    _drain(manager, task)

    assert task.status == "COMPLETED", task.reason_for_stop


def test_a_task_that_fails_still_releases_the_orb(tmp_path):
    seen = []
    manager = _manager(
        tmp_path, executor=lambda tool, args, meta: "Error: it went wrong")
    manager.set_on_activity(lambda busy: seen.append(busy))

    task = manager.submit("Doomed",
                          [{"tool": "web_search", "args": {"query": "x"}}])
    _drain(manager, task, settle=seen)

    assert task.status == "FAILED"
    assert seen[-1] is False, f"a failed task left the orb showing work: {seen}"


# ── the surface ─────────────────────────────────────────────────────────────


def test_the_desktop_wires_task_activity_to_the_surface():
    """The signal exists only if something publishes it."""
    import inspect

    import nova

    source = inspect.getsource(nova._start_ambient_intelligence)
    assert "set_on_activity" in source, (
        "nothing observes the task manager's activity, so background work "
        "stays invisible however well the orb could show it"
    )
    assert "task_activity" in source, (
        "the activity signal is observed but never published to the surface"
    )


def test_the_activity_publisher_survives_a_missing_bridge():
    """Runs on the worker thread; the desk bridge may not be up."""
    import inspect

    import nova

    source = inspect.getsource(nova._start_ambient_intelligence)
    activity = source[source.index("def _task_activity"):]
    activity = activity[:activity.index("nova_state._task_manager.set_on_activity")]
    assert "except Exception" in activity, (
        "a failure to draw the orb would propagate into the task worker"
    )
