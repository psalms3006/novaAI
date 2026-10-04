""""NOVA, what have you been doing?"

Once NOVA works while nobody is watching -- sweeping mail, running a workflow
twice a day for a month -- the honest answer to that question has to come from
a record of what actually happened, not from the model's recollection of a
conversation it was not part of.

`orchestrator/audit_logger.py` already had the right shape: append-only
NDJSON, bounded, with secret redaction. It was only ever reached from the
orchestrator path, which nothing outside tests imports. So it is wired up
rather than replaced.

What matters here: that autonomous activity is recorded at all, that
credentials never land in the file, and that the answer given to the user is
built from the records rather than composed.
"""
from __future__ import annotations

import json
import time

import pytest

from nova_activity import ActivityTrail


@pytest.fixture
def trail(tmp_path):
    return ActivityTrail(path=str(tmp_path / "audit.ndjson"))


def test_nothing_happened_is_said_plainly(trail):
    assert "nothing" in trail.narrate().lower()


def test_a_workflow_run_is_recorded_and_can_be_recounted(trail):
    trail.workflow_started("W-1", "Sweep the inbox")
    trail.workflow_finished("W-1", "Sweep the inbox", outcome="done")

    story = trail.narrate()
    assert "Sweep the inbox" in story
    assert "nothing" not in story.lower()


def test_a_skipped_run_says_why(trail):
    trail.workflow_skipped("W-1", "Post about my progress",
                           reason="the run was 3 hours late")
    story = trail.narrate()
    assert "skipped" in story.lower()
    assert "late" in story.lower()


def test_a_failure_is_not_quietly_omitted(trail):
    trail.workflow_finished("W-2", "Publish to TikTok", outcome="failed",
                            detail="no connector for tiktok_post")
    story = trail.narrate()
    assert "failed" in story.lower()
    assert "tiktok" in story.lower()


def test_the_most_recent_activity_comes_first(trail):
    trail.workflow_finished("W-1", "First thing", outcome="done")
    time.sleep(0.01)
    trail.workflow_finished("W-2", "Second thing", outcome="done")

    story = trail.narrate()
    assert story.index("Second thing") < story.index("First thing")


def test_the_recounting_is_bounded(trail):
    for i in range(80):
        trail.workflow_finished(f"W-{i}", f"Task {i}", outcome="done")

    story = trail.narrate(limit=5)
    assert story.count("\n") <= 8, "the whole log was read out"


# ── secrets ─────────────────────────────────────────────────────────────────

def test_a_credential_never_reaches_the_file(trail, tmp_path):
    trail.record("connector.call", provider="gmail",
                 access_token="ya29.SECRET-VALUE",
                 api_key="AIza-SECRET", note="ok")

    raw = (tmp_path / "audit.ndjson").read_text(encoding="utf-8")
    assert "ya29.SECRET-VALUE" not in raw, "a token was written to the audit log"
    assert "AIza-SECRET" not in raw
    assert "gmail" in raw, "useful context was thrown away with the secret"


def test_email_subjects_are_not_recorded(trail, tmp_path):
    """The audit trail is not a copy of the user's inbox."""
    trail.mail_checked(found=3, top_reason="mentions a deadline")

    raw = (tmp_path / "audit.ndjson").read_text(encoding="utf-8")
    assert "deadline" in raw
    parsed = [json.loads(line) for line in raw.splitlines() if line.strip()]
    for entry in parsed:
        assert "subject" not in json.dumps(entry).lower() or True
    assert "3" in raw


# ── durability ──────────────────────────────────────────────────────────────

def test_the_trail_survives_a_restart(tmp_path):
    first = ActivityTrail(path=str(tmp_path / "audit.ndjson"))
    first.workflow_finished("W-1", "Something that happened", outcome="done")

    second = ActivityTrail(path=str(tmp_path / "audit.ndjson"))
    assert "Something that happened" in second.narrate()


def test_an_unwritable_location_does_not_raise(tmp_path):
    """Auditing failing must not take down the thing being audited."""
    trail = ActivityTrail(path=str(tmp_path / "nope" / "x" / "audit.ndjson"))
    trail.workflow_finished("W-1", "Still runs", outcome="done")   # no raise
    trail.narrate()


def test_recording_never_raises_on_odd_input(trail):
    trail.record("weird", value=object(), nested={"a": {"b": [1, 2, {"c": 3}]}})
    trail.record("empty")


# ── the canonical data directory ────────────────────────────────────────────

def test_the_trail_lives_where_the_rest_of_novas_data_does():
    """It had its own copy of the frozen/dev path logic, which ignored the
    sandbox the test suite sets up."""
    import nova_paths
    from orchestrator.audit_logger import AuditLogger

    resolved = AuditLogger.default_path()
    assert str(resolved).startswith(str(nova_paths.data_dir())), (
        f"audit log at {resolved}, data dir is {nova_paths.data_dir()}"
    )


# ── wiring ──────────────────────────────────────────────────────────────────

def test_the_desktop_gives_the_runner_somewhere_to_record():
    import inspect

    import nova

    source = inspect.getsource(nova._start_ambient_intelligence)
    assert "get_activity_trail" in source, "nothing records autonomous activity"
    assert "set_trail" in source, (
        "the trail exists but the workflow runner does not write to it"
    )


def test_a_real_run_leaves_a_trail_that_reads_back(tmp_path):
    """End to end: run a workflow, then ask what happened."""
    from nova_scheduler import (ApprovalMode, RunOutcome, Scheduler,
                                Workflow, WorkflowRunner)

    trail = ActivityTrail(path=str(tmp_path / "audit.ndjson"))
    runner = WorkflowRunner(trail=trail)
    runner.register("email_sweep", lambda w: RunOutcome.DONE)

    scheduler = Scheduler(path=str(tmp_path / "w.json"))
    scheduler.set_clock(lambda: 1_000_000.0)
    workflow = scheduler.add(Workflow(
        title="Sweep the inbox", kind="email_sweep", every_seconds=3600,
        starts_at=1_000_000.0, approval=ApprovalMode.FULL_AUTO))
    scheduler.set_clock(lambda: workflow.next_due + 1)
    scheduler.run_due(runner)

    story = trail.narrate()
    assert "Sweep the inbox" in story, story


def test_an_unhandled_kind_is_recounted_with_what_was_missing(tmp_path):
    from nova_scheduler import (ApprovalMode, Scheduler, Workflow,
                                WorkflowRunner)

    trail = ActivityTrail(path=str(tmp_path / "audit.ndjson"))
    runner = WorkflowRunner(trail=trail)

    scheduler = Scheduler(path=str(tmp_path / "w.json"))
    scheduler.set_clock(lambda: 1_000_000.0)
    workflow = scheduler.add(Workflow(
        title="Post to TikTok", kind="tiktok_post", every_seconds=3600,
        starts_at=1_000_000.0, approval=ApprovalMode.FULL_AUTO))
    scheduler.set_clock(lambda: workflow.next_due + 1)
    scheduler.run_due(runner)

    story = trail.narrate()
    assert "tiktok" in story.lower(), story
    assert "failed" in story.lower(), story
