"""A task given as a goal, without steps, is planned -- not silently dropped.

From the real app (2026-09-26): the model called nova_task with a title and no
steps; the task manager answered "nova_task submit needs a 'steps' list ...
Nothing was started", while NOVA had just told the user she was on it.
"""
from __future__ import annotations

from task_manager import TaskManager


def _manager(tmp_path, planner):
    m = TaskManager(path=str(tmp_path / "tasks.json"), tool_executor=lambda tool, args, meta: "ok")
    m.planner = planner
    return m


def test_a_goal_without_steps_is_planned_and_queued(tmp_path):
    seen = []
    m = _manager(tmp_path, lambda goal: seen.append(goal) or [{"tool": "web_search", "args": {"query": goal}}])
    reply = m.exec_command("submit", title="Research exoplanet discoveries")
    assert seen == ["Research exoplanet discoveries"]
    assert "queued" in reply and "Nothing was started" not in reply
    [task] = m._tasks
    assert task.steps[0].tool == "web_search"


def test_given_steps_are_used_as_they_are(tmp_path):
    m = _manager(tmp_path, lambda goal: (_ for _ in ()).throw(AssertionError("planned needlessly")))
    reply = m.exec_command("submit", title="t", steps=[{"tool": "generate_document", "args": {"title": "t"}}])
    assert "queued" in reply


def test_nothing_to_plan_still_says_so(tmp_path):
    m = _manager(tmp_path, lambda goal: [])
    assert "Nothing was started" in m.exec_command("submit", title="")
    assert "Nothing was started" in m.exec_command("submit", title="do something unplannable")


def test_a_failing_planner_does_not_crash_the_tool(tmp_path):
    m = _manager(tmp_path, lambda goal: 1 / 0)
    assert "Nothing was started" in m.exec_command("submit", title="anything")


def test_the_default_planner_turns_a_plan_into_steps(tmp_path, monkeypatch):
    import types, sys
    fake = types.SimpleNamespace(
        create_plan=lambda goal: {"steps": [{"step": 1, "tool": "web_search", "description": "d", "parameters": {"query": goal}}]},
        _fallback_plan=lambda goal: {"steps": []})
    monkeypatch.setitem(sys.modules, "agent.planner", fake)
    import agent
    monkeypatch.setattr(agent, "planner", fake, raising=False)
    m = TaskManager(path=str(tmp_path / "tasks.json"), tool_executor=lambda *a: "ok")
    assert m.plan_steps("find X") == [{"tool": "web_search", "args": {"query": "find X"}}]
