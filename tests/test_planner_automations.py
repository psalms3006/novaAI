"""Reminders that arrive, and automations on a schedule (the Zoey audit's
create_recurring_task, built on NOVA's own nova_scheduler)."""
import threading
from datetime import datetime, timedelta

import pytest

import nova  # noqa: F401  (import order: nova before planner_extra)
import nova_state
import planner_extra as pe
from nova_scheduler import Scheduler, WorkflowStatus


@pytest.fixture
def sched(tmp_path, monkeypatch):
    s = Scheduler(path=str(tmp_path / "workflows.json"))
    monkeypatch.setattr(nova_state, "_scheduler", s, raising=False)
    monkeypatch.setattr(pe, "PLANNER_FILE", tmp_path / "nova_tasks.json")
    planner = pe.NOVAPlanner.__new__(pe.NOVAPlanner)   # no background checker thread
    planner._tasks, planner._lock, planner._speak_fn = [], threading.Lock(), None
    monkeypatch.setattr(nova_state, "_planner", planner, raising=False)
    return s


def test_tomorrow_at_a_time_is_tomorrow():
    tomorrow = (datetime.now() + timedelta(days=1)).date()
    due = pe.NOVAPlanner._parse_time("tomorrow at 8pm")
    assert due.date() == tomorrow and (due.hour, due.minute) == (20, 0)
    due = pe.NOVAPlanner._parse_time("tomorrow 7:30 am")
    assert due.date() == tomorrow and (due.hour, due.minute) == (7, 30)


@pytest.mark.parametrize("text,seconds,weekdays", [
    ("daily", 86400, False), ("every day", 86400, False), ("weekdays", 86400, True),
    ("every weekday", 86400, True), ("hourly", 3600, False), ("weekly", 604800, False),
    ("every 30 minutes", 1800, False), ("every 2 hours", 7200, False), ("", None, False),
])
def test_repeat_phrases(text, seconds, weekdays):
    assert pe.parse_every(text) == ((float(seconds) if seconds else None), weekdays)


def test_a_repeating_reminder_is_a_persisted_workflow(sched, tmp_path):
    out = pe._execute_planner({"action": "add", "description": "drink water",
                               "time": "in 10 minutes", "every": "hourly"})
    assert out.startswith("Scheduled (W-")
    w = sched.workflows()[0]
    assert (w.kind, w.every_seconds, w.params["text"]) == (pe.REMINDER_KIND, 3600.0, "drink water")
    # survives a restart: a new scheduler on the same file sees it
    again = Scheduler(path=str(tmp_path / "workflows.json")).workflows()
    assert [x.title for x in again] == ["drink water"]


def test_a_scheduled_task_runs_a_goal(sched):
    out = pe._execute_planner({"action": "add", "description": "AI news brief", "time": "at 8am",
                               "every": "weekdays", "run": "research today's AI news and summarise it"})
    assert "I'll do this each time" in out and "every weekday" in out
    w = sched.workflows()[0]
    assert w.kind == pe.GOAL_KIND and w.params["weekdays_only"] is True
    assert w.params["goal"].startswith("research today's AI news")


def test_list_pause_resume_cancel(sched):
    pe._execute_planner({"action": "add", "description": "stand up", "every": "every 45 minutes"})
    pe._execute_planner({"action": "add", "description": "call mum", "time": "in 5 minutes"})
    listed = pe._execute_planner({"action": "list"})
    assert "call mum" in listed and "repeating reminder: stand up" in listed
    assert pe._execute_planner({"action": "pause", "task_id": "stand up"}).startswith("Paused")
    assert sched.workflows()[0].status == WorkflowStatus.PAUSED
    assert pe._execute_planner({"action": "resume", "task_id": "stand up"}).startswith("Resumed")
    assert pe._execute_planner({"action": "cancel", "task_id": "stand up"}).startswith("Cancelled")
    assert sched.workflows()[0].status == WorkflowStatus.CANCELLED
    assert pe._execute_planner({"action": "cancel", "task_id": "call mum"}) == "Cancelled: call mum"


def test_a_due_one_shot_reminder_is_delivered_not_printed(sched, monkeypatch):
    said = []
    planner = nova_state._planner
    planner.set_speak(said.append)
    planner._tasks.append({"id": "1", "description": "take the cake out",
                           "due": (datetime.now() - timedelta(seconds=1)).isoformat(), "done": False})
    calls = {"n": 0}

    def once(_s):                       # one pass of the checker loop, then stop
        calls["n"] += 1
        if calls["n"] > 1:
            raise StopIteration
    monkeypatch.setattr(pe.time, "sleep", once)
    with pytest.raises(StopIteration):
        planner._check_loop()
    assert said == ["Reminder: take the cake out"]
