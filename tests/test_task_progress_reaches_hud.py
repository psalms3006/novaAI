"""A background task's progress has to survive a restart and reach the screen.

NOVA tells the user "give me about ten minutes" and submits a task. Three
things have to hold for the user to see that honestly:

* the estimate travels from the model, through nova_task, onto the Task;
* a task reloaded from disk is still a working Task -- to_dict() writes the
  computed ``seconds_remaining`` alongside real fields, and from_dict() used
  to setattr() any key the object ``hasattr``, replacing the method with
  ``None`` so the next to_dict() raised "'NoneType' object is not callable";
* /api/system reports the task under the key the HUD reads, with a status
  the HUD recognises. It used to read ``task_id`` (the dict says ``id``) and
  the HUD looked for lowercase "running" while tasks say "RUNNING", so the
  OBJECTIVE line never showed any running task at all.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from task_manager import Task, TaskManager, TaskStep

# The checks on the previous window (desk/static) that lived here moved to
# tests/test_desk_ui_behaviour.py when the interface became desk/ui.

REPO = Path(__file__).resolve().parents[1]


def _running(estimate: float, elapsed: float) -> Task:
    t = Task(title="Research", steps=[TaskStep(tool="web_search", args={})],
             estimated_duration_s=estimate)
    t.status = "RUNNING"
    t.started = time.time() - elapsed
    return t


def _manager(tmp_path, name="t.json"):
    return TaskManager(path=str(tmp_path / name), tool_executor=lambda *a: "ok")


def test_a_task_reloaded_from_disk_still_serialises():
    original = _running(600, 60)
    reloaded = Task.from_dict(original.to_dict())

    assert callable(reloaded.seconds_remaining)
    d = reloaded.to_dict()
    assert d["estimated_duration_s"] == 600
    assert 500 < d["seconds_remaining"] <= 540


def test_a_manager_restarted_mid_task_can_still_list_it(tmp_path):
    first = _manager(tmp_path)
    first.submit("Research", [{"tool": "web_search", "args": {"query": "x"}}],
                 estimated_duration_s=300)

    second = _manager(tmp_path)
    [task] = second.list()
    assert task.estimated_duration_s == 300
    assert "Research" in second.exec_command("list")


def test_time_based_progress_never_claims_completion_early():
    assert 45 <= _running(100, 50).progress_percent() <= 55
    assert _running(10, 500).progress_percent() == 95
    assert _running(10, 500).seconds_remaining() == 0.0


def test_the_model_is_told_it_can_pass_an_estimate():
    import nova
    decl = next(d for d in nova.TOOL_DECLARATIONS if d["name"] == "nova_task")
    told = decl["description"] + decl["parameters"]["properties"]["args"]["description"]
    assert "estimated_duration_s" in told


def test_the_estimate_reaches_the_task_through_the_tool(monkeypatch, tmp_path):
    import nova
    import nova_state
    manager = _manager(tmp_path)
    monkeypatch.setattr(nova_state, "_task_manager", manager, raising=False)

    nova._execute_tool_sync("nova_task", {"cmd": "submit", "args": {
        "title": "Research", "estimated_duration_s": 600,
        "steps": [{"tool": "web_search", "args": {"query": "x"}}]}}, {})

    [task] = manager.list()
    assert task.estimated_duration_s == 600


def _running_in_manager(tmp_path):
    manager = _manager(tmp_path)
    task = manager.submit("Research", [{"tool": "web_search", "args": {"query": "x"}}],
                          estimated_duration_s=100)
    task.status = "RUNNING"
    task.started = time.time() - 50
    return manager, task


def _get(monkeypatch, manager, path):
    from desk import bridge
    monkeypatch.setattr(bridge, "_ns", lambda name, default=None:
                        manager if name == "_task_manager" else default)
    return bridge.app.test_client().get(
        path, headers={"X-NOVA-Desk": bridge.run_token}).get_json()


def test_the_hud_snapshot_carries_id_progress_and_countdown(monkeypatch, tmp_path):
    manager, task = _running_in_manager(tmp_path)

    [entry] = _get(monkeypatch, manager, "/api/system")["tasks"]
    assert entry["id"] == task.id
    assert entry["status"] == "RUNNING"
    assert 45 <= entry["progress"] <= 55
    assert 45 <= entry["seconds_remaining"] <= 55


def test_the_task_list_endpoint_returns_computed_progress(monkeypatch, tmp_path):
    manager, _ = _running_in_manager(tmp_path)

    [entry] = _get(monkeypatch, manager, "/api/tasks")["tasks"]
    assert 45 <= entry["progress"] <= 55
    assert entry["seconds_remaining"] is not None


