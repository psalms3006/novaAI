"""A task given as a goal, without steps, is planned -- not silently dropped,
and not planned on the voice turn that asked for it.

From the real app (2026-09-26): the model called nova_task with a title and no
steps; the task manager answered "nova_task submit needs a 'steps' list ...
Nothing was started", while NOVA had just told the user she was on it. The
first fix planned the goal inside the tool call, which held the voice turn
silent for as long as the planner (a model call) took. Now a worker plans it
and the call returns at once.
"""
from __future__ import annotations

import time

from task_manager import TERMINAL, TaskManager


def _manager(tmp_path, planner, executor=lambda tool, args, meta: "Results: ok"):
    m = TaskManager(path=str(tmp_path / "tasks.json"), tool_executor=executor)
    m.planner = planner
    return m


def _settle(m, task, timeout=10.0):
    m.start()
    try:
        deadline = time.time() + timeout
        while task.status not in TERMINAL and time.time() < deadline:
            time.sleep(0.02)
        return task
    finally:
        m.stop()


def test_the_call_returns_before_planning_happens(tmp_path):
    seen = []
    m = _manager(tmp_path, lambda goal: seen.append(goal) or [{"tool": "web_search", "args": {"query": goal}}])
    t0 = time.monotonic()
    reply = m.exec_command("submit", title="Research exoplanet discoveries")
    assert time.monotonic() - t0 < 0.5
    assert seen == [], "the goal was planned on the caller's thread"
    assert "queued" in reply and "Nothing was started" not in reply
    [task] = m.list()
    assert task.needs_plan and task.status == "QUEUED"


def test_a_worker_plans_the_goal_and_runs_it(tmp_path):
    m = _manager(tmp_path, lambda goal: [{"tool": "web_search", "args": {"query": goal}}])
    m.exec_command("submit", title="Research exoplanet discoveries")
    [task] = m.list()
    _settle(m, task)
    assert task.steps[0].tool == "web_search"
    assert task.steps[0].args == {"query": "Research exoplanet discoveries"}
    assert task.status == "COMPLETED", task.reason_for_stop
    assert any(h["event"] == "task.planned" for h in task.history)


def test_given_steps_are_used_as_they_are(tmp_path):
    m = _manager(tmp_path, lambda goal: (_ for _ in ()).throw(AssertionError("planned needlessly")))
    reply = m.exec_command("submit", title="t", steps=[{"tool": "generate_document", "args": {"title": "t"}}])
    assert "queued" in reply
    assert not m.list()[0].needs_plan


def test_no_goal_still_says_nothing_was_started(tmp_path):
    m = _manager(tmp_path, lambda goal: [])
    assert "Nothing was started" in m.exec_command("submit", title="")
    assert m.list() == []


def test_an_unplannable_goal_fails_and_says_why(tmp_path):
    told = []
    m = _manager(tmp_path, lambda goal: [])
    m.set_notify(told.append)
    m.exec_command("submit", title="do something unplannable")
    [task] = m.list()
    _settle(m, task)
    assert task.status == "FAILED"
    assert "could not plan" in task.reason_for_stop
    deadline = time.time() + 5            # told on the worker, after the status
    while not told and time.time() < deadline:
        time.sleep(0.02)
    assert told and "couldn't finish" in told[0] and "could not plan" in told[0]


def test_a_failing_planner_does_not_crash_the_worker(tmp_path):
    m = _manager(tmp_path, lambda goal: 1 / 0)
    m.exec_command("submit", title="anything")
    [task] = m.list()
    _settle(m, task)
    assert task.status == "FAILED" and "could not plan" in task.reason_for_stop


def test_the_default_planner_turns_a_plan_into_steps(tmp_path, monkeypatch):
    import types, sys
    fake = types.SimpleNamespace(
        create_plan=lambda goal, **kw: {"steps": [{"step": 1, "tool": "web_search", "description": "d", "parameters": {"query": goal}}]},
        _fallback_plan=lambda goal: {"steps": []})
    monkeypatch.setitem(sys.modules, "agent.planner", fake)
    import agent
    monkeypatch.setattr(agent, "planner", fake, raising=False)
    m = TaskManager(path=str(tmp_path / "tasks.json"), tool_executor=lambda *a: "ok")
    assert m.plan_steps("find X") == [{"tool": "web_search", "args": {"query": "find X"}}]


# ── the plan uses tools NOVA actually has ────────────────────────────────────
# From the live run on 2026-09-26: the planner's prompt listed an older NOVA's
# tools, and a real plan for "write me a report" came back as code_helper +
# cmd_control -- neither exists, so each step would have been refused.

def test_the_planner_is_offered_exactly_the_declared_tools():
    import nova
    from agent.planner import prompt_for_tools
    prompt = prompt_for_tools(nova.TOOL_DECLARATIONS)
    listed = {line.split(" — ")[0].strip() for line in prompt.splitlines() if " — " in line
              and not line.startswith(" ")}
    declared = {d["name"] for d in nova.TOOL_DECLARATIONS}
    assert listed - {"OUTPUT"} <= declared, sorted(listed - declared)
    for legacy in ("cmd_control", "code_helper", "send_message", "game_updater"):
        assert f"\n{legacy} " not in prompt and f"\n{legacy}\n" not in prompt
    assert "nova_task —" not in prompt, "a plan must not spawn more plans"


def test_steps_naming_tools_nova_lacks_are_dropped(tmp_path, monkeypatch):
    import sys, types
    import agent
    fake = types.SimpleNamespace(create_plan=lambda goal, **kw: {"steps": [
        {"tool": "web_search", "parameters": {"query": "q"}},
        {"tool": "cmd_control", "parameters": {"task": "open it"}}]})
    monkeypatch.setitem(sys.modules, "agent.planner", fake)
    monkeypatch.setattr(agent, "planner", fake, raising=False)
    m = TaskManager(path=str(tmp_path / "t.json"), tool_executor=lambda *a: "ok")
    assert [s["tool"] for s in m.plan_steps("find it and open it")] == ["web_search"]


def test_with_no_planner_a_write_up_is_still_planned(tmp_path, monkeypatch):
    import sys, types
    import agent
    fake = types.SimpleNamespace(create_plan=lambda goal, **kw: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setitem(sys.modules, "agent.planner", fake)
    monkeypatch.setattr(agent, "planner", fake, raising=False)
    m = TaskManager(path=str(tmp_path / "t.json"), tool_executor=lambda *a: "ok")
    assert [s["tool"] for s in m.plan_steps("write a report on comets")] == ["web_search", "generate_document"]
    assert [s["tool"] for s in m.plan_steps("what is the tallest tree")] == ["web_search"]
