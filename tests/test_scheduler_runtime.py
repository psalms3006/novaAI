"""A scheduler nothing ticks is a file, not a feature.

NOVA has form here. `core/event_bus.py` is sound and tested and has no
publishers. `ProactiveAgent` was a stub wired into a correct speech path that
never called it. Both looked finished. So the scheduler gets its runtime
connected and tested in the same breath as the scheduler itself.

What is pinned here is the loop and its edges: that ticking runs due work,
that stopping actually stops, that a workflow kind nobody handles is reported
rather than silently dropped, and that results reach the user through the one
policy that decides whether to interrupt them.
"""
from __future__ import annotations

import time

import pytest

from nova_scheduler import (
    ApprovalMode,
    RunOutcome,
    Scheduler,
    Workflow,
    WorkflowStatus,
)
from nova_scheduler import WorkflowRunner


HOUR = 3600.0
START = 1_000_000.0


@pytest.fixture
def scheduler(tmp_path):
    s = Scheduler(path=str(tmp_path / "workflows.json"))
    s.set_clock(lambda: START)
    return s


def _workflow(**kw):
    defaults = dict(title="Sweep the inbox", kind="email_sweep",
                    every_seconds=HOUR, starts_at=START,
                    approval=ApprovalMode.FULL_AUTO)
    defaults.update(kw)
    return Workflow(**defaults)


# ── the runner ──────────────────────────────────────────────────────────────

def test_a_registered_handler_receives_its_workflow(scheduler):
    runner = WorkflowRunner()
    seen = []
    runner.register("email_sweep", lambda w: seen.append(w.title) or RunOutcome.DONE)

    workflow = scheduler.add(_workflow())
    scheduler.set_clock(lambda: workflow.next_due + 1)
    scheduler.run_due(runner)

    assert seen == ["Sweep the inbox"]


def test_a_kind_nobody_handles_is_reported_not_swallowed(scheduler):
    """The silent-drop failure: a workflow that runs forever doing nothing."""
    runner = WorkflowRunner()
    workflow = scheduler.add(_workflow(kind="tiktok_post"))
    scheduler.set_clock(lambda: workflow.next_due + 1)

    tally = scheduler.run_due(runner)

    assert tally["failed"] == 1, (
        "a workflow with no handler was treated as a successful run"
    )
    assert runner.unhandled_kinds() == {"tiktok_post"}, (
        "nothing recorded which capability was missing"
    )


def test_an_unhandled_kind_does_not_retry_forever(scheduler):
    """No connector is coming; stop rather than grinding."""
    runner = WorkflowRunner()
    workflow = scheduler.add(_workflow(kind="tiktok_post"))

    for _ in range(10):
        scheduler.set_clock(lambda: workflow.next_due + 1)
        scheduler.run_due(runner)
        if workflow.status is not WorkflowStatus.ACTIVE:
            break

    assert workflow.status is WorkflowStatus.FAILED


def test_a_handler_that_raises_is_a_failure_not_a_crash(scheduler):
    runner = WorkflowRunner()
    runner.register("email_sweep",
                    lambda w: (_ for _ in ()).throw(RuntimeError("api down")))

    workflow = scheduler.add(_workflow())
    scheduler.set_clock(lambda: workflow.next_due + 1)
    tally = scheduler.run_due(runner)

    assert tally["failed"] == 1
    assert workflow.status is WorkflowStatus.ACTIVE, "one failure ended it"


# ── the loop ────────────────────────────────────────────────────────────────

def test_ticking_runs_what_is_due_and_stopping_stops(tmp_path):
    scheduler = Scheduler(path=str(tmp_path / "w.json"))
    runner = WorkflowRunner()
    ran = []
    runner.register("email_sweep", lambda w: ran.append(1) or RunOutcome.DONE)

    scheduler.add(Workflow(title="t", kind="email_sweep",
                           every_seconds=0.05, starts_at=time.time(),
                           approval=ApprovalMode.FULL_AUTO))

    scheduler.start(runner, interval_seconds=0.02)
    try:
        deadline = time.time() + 5
        while time.time() < deadline and not ran:
            time.sleep(0.01)
    finally:
        scheduler.stop()

    assert ran, "the loop never ran anything"

    settled = len(ran)
    time.sleep(0.2)
    assert len(ran) == settled, "work continued after stop()"


def test_the_loop_survives_a_handler_that_keeps_failing(tmp_path):
    scheduler = Scheduler(path=str(tmp_path / "w.json"))
    runner = WorkflowRunner()
    calls = []

    def bad(w):
        calls.append(1)
        raise RuntimeError("still broken")

    runner.register("email_sweep", bad)
    scheduler.add(Workflow(title="t", kind="email_sweep",
                           every_seconds=0.05, starts_at=time.time(),
                           approval=ApprovalMode.FULL_AUTO))

    scheduler.start(runner, interval_seconds=0.02)
    try:
        time.sleep(0.4)
    finally:
        scheduler.stop()

    assert calls, "the loop died on the first failure"


def test_starting_twice_does_not_make_two_loops(tmp_path):
    scheduler = Scheduler(path=str(tmp_path / "w.json"))
    runner = WorkflowRunner()
    scheduler.start(runner, interval_seconds=0.05)
    first = scheduler._thread
    scheduler.start(runner, interval_seconds=0.05)
    try:
        assert scheduler._thread is first, "a second loop was started"
    finally:
        scheduler.stop()


# ── telling the user ────────────────────────────────────────────────────────

def test_a_completed_run_is_offered_to_the_user(scheduler):
    """Results reach the one policy that decides whether to interrupt."""
    from nova_proactive import ProactiveAgent

    said = []
    agent = ProactiveAgent(speak_fn=said.append)

    runner = WorkflowRunner()
    runner.register("email_sweep", lambda w: RunOutcome.DONE)
    runner.set_proactive(agent)

    workflow = scheduler.add(_workflow())
    scheduler.set_clock(lambda: workflow.next_due + 1)
    scheduler.run_due(runner)

    assert said, "a finished workflow told nobody"


def test_a_run_with_nothing_to_do_stays_quiet(scheduler):
    """Skipping a post is allowed; announcing every skip is not."""
    from nova_proactive import ProactiveAgent

    said = []
    agent = ProactiveAgent(speak_fn=said.append)

    runner = WorkflowRunner()
    runner.register("email_sweep", lambda w: RunOutcome.NOTHING_TO_DO)
    runner.set_proactive(agent)

    workflow = scheduler.add(_workflow())
    scheduler.set_clock(lambda: workflow.next_due + 1)
    scheduler.run_due(runner)

    assert said == [], f"narrated an uneventful run: {said}"


# ── wiring: the loop has to actually be started by the app ─────────────────

def test_the_desktop_starts_the_scheduler():
    """The orphan check.

    core/event_bus.py is sound, tested, and has no publishers. ProactiveAgent
    was a correct speech path wired to a stub. Both looked finished. A
    scheduler nothing ticks would be the third.
    """
    import inspect

    import nova

    source = inspect.getsource(nova._start_ambient_intelligence)
    assert "get_scheduler" in source, "nothing starts the scheduler"
    assert "scheduler.start(" in source, (
        "the scheduler is created but never ticked, so no workflow ever runs"
    )
    assert "set_proactive" in source, (
        "workflow results have no route to the user"
    )


def test_a_scheduler_failure_reports_itself_accurately():
    """A wrong log line costs someone an hour later."""
    import inspect

    import nova

    source = inspect.getsource(nova._start_ambient_intelligence)
    block = source[source.index("Scheduled workflows"):]
    assert "Scheduler failed to start" in block, (
        "the scheduler's failure path reports someone else's error message"
    )
