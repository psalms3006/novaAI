"""The model has to be told the step shape the task manager actually reads.

`nova_task` is how a spoken request becomes sustained background work: the
model authors the steps and submits them. It is the planner, so the schema it
is shown has to be the schema the code consumes.

It was not. The declaration said:

    Steps are [{tool, arg, verify}]

while `task_manager.submit` reads `s["tool"]` and `s.get("args", {})` and has
no `verify` field at all. A model following the documented schema got
`args={}` on every step -- a task that runs and does nothing -- and a step
missing "tool" raised a KeyError surfaced as the opaque "nova_task error:
'tool'".

This is the drift that made "one request, then NOVA works on it" impossible,
so it is worth a test rather than a one-off correction.
"""
from __future__ import annotations

import re

import pytest

import nova


def _nova_task_declaration() -> dict:
    for decl in nova.TOOL_DECLARATIONS:
        if decl.get("name") == "nova_task":
            return decl
    pytest.fail("nova_task is no longer declared to the model")


def _keys_the_implementation_reads() -> set[str]:
    """Derived from task_manager.submit, not from a hardcoded list."""
    import inspect
    from task_manager import TaskManager

    source = inspect.getsource(TaskManager.submit)
    subscripted = set(re.findall(r's\["([a-z_]+)"\]', source))
    fetched = set(re.findall(r's\.get\("([a-z_]+)"', source))
    return subscripted | fetched


def test_the_declared_step_keys_are_the_ones_the_code_reads():
    """Fails while the description advertises a field submit() ignores."""
    described = _nova_task_declaration()["description"]

    # The step shape as documented to the model, e.g. "[{tool, args}]".
    shapes = re.findall(r"\[\s*\{([^}]*)\}\s*\]", described)
    assert shapes, (
        f"the description no longer shows the model a step shape: {described!r}"
    )
    documented = {
        token.strip().strip('"\'')
        for token in re.split(r"[,\s]+", shapes[0])
        if token.strip()
    }

    actual = _keys_the_implementation_reads()

    invented = documented - actual
    assert not invented, (
        f"the model is told to send {sorted(invented)}, which "
        f"task_manager.submit never reads. It reads {sorted(actual)}."
    )


def test_a_step_written_to_the_documented_shape_keeps_its_arguments(tmp_path):
    """The failure this drift caused: arguments silently dropped."""
    from task_manager import TaskManager

    manager = TaskManager(
        path=str(tmp_path / "tasks.json"),
        tool_executor=lambda tool, args, meta: "ok",
    )
    task = manager.submit(
        "Research something",
        [{"tool": "web_search", "args": {"query": "recent AI developments"}}],
    )

    assert task.steps[0].tool == "web_search"
    assert task.steps[0].args == {"query": "recent AI developments"}, (
        "the step arrived with its arguments dropped"
    )


def test_the_worked_example_is_valid_json_the_task_manager_accepts():
    """An example is a demonstration; a malformed one teaches malformed output.

    The first version of this description contained
    '"title": "Research AI "\\ndevelopments"' — a string closed early by a
    stray quote from a line continuation. It looked fine in the source and
    would have taught the model to emit broken JSON.
    """
    import json

    from task_manager import TaskManager

    described = _nova_task_declaration()["description"]
    start = described.index("Example:") + len("Example:")
    candidate = described[start:].strip()
    # Trim the trailing prose after the JSON object.
    depth = 0
    for index, char in enumerate(candidate):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                candidate = candidate[: index + 1]
                break

    payload = json.loads(candidate)          # raises if the example is broken

    assert payload["cmd"] == "submit"
    steps = payload["args"]["steps"]
    assert steps, "the example submits no steps"
    for step in steps:
        assert "tool" in step and "args" in step, step
        assert isinstance(step["args"], dict), step

    # And the example is not merely well-formed — it is acceptable.
    import tempfile
    manager = TaskManager(
        path=str(tempfile.mkdtemp() + "/tasks.json"),
        tool_executor=lambda tool, args, meta: "ok",
    )
    task = manager.submit(payload["args"]["title"], steps)
    assert len(task.steps) == len(steps)
    assert task.steps[0].args, "the example's arguments were dropped"


def test_the_model_is_told_which_tools_a_step_may_name():
    """A planner that does not know the vocabulary cannot plan.

    Every tool named as an example must be one the permission engine can
    authorise, or the step is refused at execution time and the task is
    reported to the user as failed.
    """
    from nova_core.permissions import capabilities_for_tool

    described = _nova_task_declaration()["description"]
    quoted = set(re.findall(r"'([a-z_]+)'", described))

    commands = {"submit", "status", "pause", "resume", "cancel", "list"}
    candidates = quoted - commands

    named_tools = {
        name for name in candidates
        if any(d.get("name") == name for d in nova.TOOL_DECLARATIONS)
    }
    assert named_tools, (
        "the description names no real tool, so the model has to guess what "
        "a step may contain"
    )

    undeclared = sorted(t for t in named_tools if capabilities_for_tool(t) is None)
    assert undeclared == [], (
        f"the description suggests tool(s) the permission engine will refuse: "
        f"{undeclared}"
    )
