"""Workflows that outlive the conversation that created them.

Phase 3. "Post twice a day for the next month" cannot live in a timer inside
the process that heard the request: NOVA gets closed, the machine reboots, and
a month-long commitment evaporates silently. So the schedule is persisted and
the next due time is a stored fact, recomputed from the clock rather than
counted down in memory.

The part worth most care is what happens when NOVA was not running at the
moment something was due. Publishing everything that was missed the instant
she starts is how an assistant posts eleven times in one minute. Lateness is
therefore graded: a little late is still on time, very late is worth
reconsidering, and past the end of the workflow is over.

Tests drive a frozen clock, so none of this waits on real time.
"""
from __future__ import annotations

import pytest

from nova_scheduler import (
    ApprovalMode,
    RunOutcome,
    Scheduler,
    Workflow,
    WorkflowStatus,
)


HOUR = 3600.0
DAY = 24 * HOUR
START = 1_000_000.0


@pytest.fixture
def scheduler(tmp_path):
    s = Scheduler(path=str(tmp_path / "workflows.json"))
    s.set_clock(lambda: START)
    return s


def _at(scheduler, when):
    scheduler.set_clock(lambda: when)


def _workflow(**kw):
    defaults = dict(
        title="Post about my progress",
        kind="social_post",
        every_seconds=12 * HOUR,
        starts_at=START,
        ends_at=START + 30 * DAY,
        approval=ApprovalMode.APPROVE_BEFORE_PUBLISH,
    )
    defaults.update(kw)
    return Workflow(**defaults)


# ── persistence ─────────────────────────────────────────────────────────────

def test_a_workflow_survives_a_restart(scheduler, tmp_path):
    scheduler.add(_workflow())

    reopened = Scheduler(path=str(tmp_path / "workflows.json"))
    reopened.set_clock(lambda: START)

    assert len(reopened.workflows()) == 1
    assert reopened.workflows()[0].title == "Post about my progress"


def test_the_next_due_time_survives_a_restart(scheduler, tmp_path):
    workflow = scheduler.add(_workflow())
    original = workflow.next_due

    reopened = Scheduler(path=str(tmp_path / "workflows.json"))
    reopened.set_clock(lambda: START)
    assert reopened.workflows()[0].next_due == original


# ── due-ness ────────────────────────────────────────────────────────────────

def test_nothing_is_due_before_its_start(scheduler):
    """`starts_at` means what it says: the first run happens then, not before.

    A caller that wants the first run delayed sets a later start. The
    scheduler does not second-guess it by silently adding an interval, which
    would make "start at nine" mean "start at nine tonight".
    """
    scheduler.add(_workflow(starts_at=START + DAY))
    _at(scheduler, START + HOUR)
    assert scheduler.due() == []

    _at(scheduler, START + DAY + 1)
    assert len(scheduler.due()) == 1, "the workflow never started"


def test_a_workflow_becomes_due_when_its_time_arrives(scheduler):
    workflow = scheduler.add(_workflow())
    _at(scheduler, workflow.next_due + 1)
    assert [w.id for w in scheduler.due()] == [workflow.id]


def test_running_moves_the_next_due_time_forward(scheduler):
    workflow = scheduler.add(_workflow())
    _at(scheduler, workflow.next_due + 1)

    scheduler.run_due(lambda w: RunOutcome.DONE)

    assert scheduler.due() == [], "the same run came due twice"
    assert workflow.next_due > START


# ── lateness, the part that prevents a burst of stale posts ─────────────────

def test_slightly_late_still_runs(scheduler):
    workflow = scheduler.add(_workflow())
    _at(scheduler, workflow.next_due + 10 * 60)          # ten minutes late

    ran = []
    scheduler.run_due(lambda w: ran.append(w.id) or RunOutcome.DONE)
    assert ran == [workflow.id]


def test_very_late_is_skipped_rather_than_published(scheduler):
    workflow = scheduler.add(_workflow())
    _at(scheduler, workflow.next_due + 3 * DAY)

    ran = []
    result = scheduler.run_due(lambda w: ran.append(w.id) or RunOutcome.DONE)

    assert ran == [], "three-day-old content was published as if fresh"
    assert result["skipped"] == 1
    assert scheduler.due() == [], "the stale run is still pending"


def test_a_skipped_run_does_not_end_the_workflow(scheduler):
    workflow = scheduler.add(_workflow())
    _at(scheduler, workflow.next_due + 3 * DAY)
    scheduler.run_due(lambda w: RunOutcome.DONE)

    assert workflow.status is WorkflowStatus.ACTIVE
    assert workflow.next_due > scheduler.now()


def test_a_missed_run_is_counted(scheduler):
    workflow = scheduler.add(_workflow())
    _at(scheduler, workflow.next_due + 3 * DAY)
    scheduler.run_due(lambda w: RunOutcome.DONE)
    assert workflow.missed == 1


# ── ending ──────────────────────────────────────────────────────────────────

def test_a_workflow_expires_at_its_end_date(scheduler):
    workflow = scheduler.add(_workflow())
    _at(scheduler, START + 31 * DAY)

    scheduler.run_due(lambda w: RunOutcome.DONE)
    assert workflow.status is WorkflowStatus.EXPIRED
    assert scheduler.due() == []


def test_an_expired_workflow_never_runs_again(scheduler):
    workflow = scheduler.add(_workflow(ends_at=START + HOUR))
    _at(scheduler, START + 2 * HOUR)

    ran = []
    scheduler.run_due(lambda w: ran.append(w.id) or RunOutcome.DONE)
    _at(scheduler, START + 10 * DAY)
    scheduler.run_due(lambda w: ran.append(w.id) or RunOutcome.DONE)

    assert ran == []


# ── user control ────────────────────────────────────────────────────────────

def test_pausing_stops_it_coming_due(scheduler):
    workflow = scheduler.add(_workflow())
    scheduler.pause(workflow.id)
    _at(scheduler, workflow.next_due + 1)
    assert scheduler.due() == []


def test_resuming_brings_it_back(scheduler):
    workflow = scheduler.add(_workflow())
    scheduler.pause(workflow.id)
    scheduler.resume(workflow.id)
    _at(scheduler, workflow.next_due + 1)
    assert [w.id for w in scheduler.due()] == [workflow.id]


def test_a_paused_workflow_stays_paused_across_a_restart(scheduler, tmp_path):
    workflow = scheduler.add(_workflow())
    scheduler.pause(workflow.id)

    reopened = Scheduler(path=str(tmp_path / "workflows.json"))
    reopened.set_clock(lambda: workflow.next_due + 1)
    assert reopened.due() == []


def test_cancelling_removes_it_from_the_schedule(scheduler):
    workflow = scheduler.add(_workflow())
    scheduler.cancel(workflow.id)
    _at(scheduler, workflow.next_due + 1)

    assert scheduler.due() == []
    assert workflow.status is WorkflowStatus.CANCELLED


def test_changing_the_frequency_recomputes_the_next_run(scheduler):
    workflow = scheduler.add(_workflow(every_seconds=12 * HOUR))
    scheduler.reschedule(workflow.id, every_seconds=DAY)

    assert workflow.every_seconds == DAY
    assert workflow.next_due <= scheduler.now() + DAY


# ── approval modes ──────────────────────────────────────────────────────────

def test_draft_only_never_publishes(scheduler):
    workflow = scheduler.add(_workflow(approval=ApprovalMode.DRAFT_ONLY))
    _at(scheduler, workflow.next_due + 1)

    modes = []
    scheduler.run_due(lambda w: modes.append(w.approval) or RunOutcome.DONE)

    assert modes == [ApprovalMode.DRAFT_ONLY], (
        "the runner was not told this workflow may only draft"
    )


def test_a_paused_approval_mode_does_not_run(scheduler):
    workflow = scheduler.add(_workflow(approval=ApprovalMode.PAUSED))
    _at(scheduler, workflow.next_due + 1)
    assert scheduler.due() == []


# ── failure handling ────────────────────────────────────────────────────────

def test_a_failing_run_is_retried_with_backoff(scheduler):
    workflow = scheduler.add(_workflow())
    _at(scheduler, workflow.next_due + 1)

    scheduler.run_due(lambda w: RunOutcome.FAILED)
    assert workflow.failures == 1
    assert workflow.status is WorkflowStatus.ACTIVE
    assert workflow.next_due > scheduler.now(), "retried instantly, in a loop"


def test_retries_are_bounded(scheduler):
    workflow = scheduler.add(_workflow())
    for _ in range(12):
        _at(scheduler, workflow.next_due + 1)
        scheduler.run_due(lambda w: RunOutcome.FAILED)
        if workflow.status is not WorkflowStatus.ACTIVE:
            break

    assert workflow.status is WorkflowStatus.FAILED, (
        f"still retrying after {workflow.failures} failures"
    )


def test_a_success_clears_the_failure_count(scheduler):
    workflow = scheduler.add(_workflow())
    _at(scheduler, workflow.next_due + 1)
    scheduler.run_due(lambda w: RunOutcome.FAILED)
    _at(scheduler, workflow.next_due + 1)
    scheduler.run_due(lambda w: RunOutcome.DONE)

    assert workflow.failures == 0


def test_a_runner_that_raises_does_not_stop_the_scheduler(scheduler):
    first = scheduler.add(_workflow(title="one"))
    second = scheduler.add(_workflow(title="two"))
    _at(scheduler, first.next_due + 1)

    seen = []

    def runner(w):
        seen.append(w.title)
        if w.title == "one":
            raise RuntimeError("connector exploded")
        return RunOutcome.DONE

    result = scheduler.run_due(runner)

    assert "two" in seen, "one workflow's crash stopped the others"
    assert result["failed"] >= 1


def test_an_absurdly_tight_cadence_is_clamped(scheduler):
    """A typo, or a model inventing every_seconds=1, must not hammer an API.

    These workflows call other people's services. A cadence of seconds would
    rate-limit the user's own account, so it is clamped rather than honoured.
    """
    from nova_scheduler import MIN_INTERVAL_SECONDS, RunOutcome

    workflow = scheduler.add(_workflow(every_seconds=1.0))
    _at(scheduler, workflow.next_due + 1)
    scheduler.run_due(lambda w: RunOutcome.DONE)

    assert workflow.next_due - scheduler.now() >= MIN_INTERVAL_SECONDS - 2, (
        "a one-second workflow would run again immediately"
    )
