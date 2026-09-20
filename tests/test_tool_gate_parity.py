"""Every surface must ask permission through the same gate.

NOVA has two places a model's tool call can arrive: the typed chat box
(`/api/chat` -> `desk.chat.run_turn`) and the voice session (`/live` ->
`LiveManager`). The authorisation check and the confirmation prompt both live
in `nova._execute_tool_sync`.

The voice path calls it. The typed path calls `orch.registry.execute(...)`,
and `CapabilityRegistry.execute` tries `resolve_dynamic_handler(name)` before
it tries the `fallback_fn` that bridges to `_execute_tool_sync`. The dynamic
handler imports `actions/<tool>.py` and calls its `execute()` directly, so for
any tool that has such a module -- `file_controller` and `computer_settings`
among them, both listed in `nova_safety.CONSEQUENTIAL_TOOLS` -- the typed path
reached the action without asking anyone.

These tests pin the property that matters, on both surfaces: when the gate
says no, the action does not run.
"""
from __future__ import annotations

import pytest

import nova_core.permissions as permissions


TOOL = "file_controller"
ARGS = {"action": "delete", "path": "C:/Users/example/important.txt"}


@pytest.fixture
def denied_gate(monkeypatch):
    """Deny everything at the authorisation layer, and count the asking.

    Returns a dict with 'asked' (times the gate was consulted) and 'executed'
    (times the real action would have run). The action itself is replaced, so
    nothing on disk is ever touched even if the gate fails to hold.
    """
    record = {"asked": 0, "executed": 0}

    real_check = permissions.check_tool

    def counting_deny(principal, tool_name, trust=None, args=None, **kw):
        if tool_name == TOOL:
            record["asked"] += 1
            decision = real_check(principal, tool_name, trust=trust, args=args, **kw)
            return permissions.Decision(
                permissions.Effect.DENY, None, principal,
                "denied by this test's gate",
            )
        return real_check(principal, tool_name, trust=trust, args=args, **kw)

    monkeypatch.setattr(permissions, "check_tool", counting_deny)

    import actions.file_controller as file_controller

    def recorder(args):
        record["executed"] += 1
        return "DELETED (this is a test recorder; nothing was removed)"

    monkeypatch.setattr(file_controller, "execute", recorder, raising=False)
    return record


def test_the_voice_path_does_not_execute_a_denied_tool(denied_gate):
    """The surface that was already correct. Guards against fixing by removal."""
    import nova

    result = nova._execute_tool_sync(TOOL, ARGS, {})

    assert denied_gate["asked"] >= 1, "voice path never consulted the gate"
    assert denied_gate["executed"] == 0, "a denied tool ran anyway"
    assert "refused" in result.lower()


def test_the_typed_chat_path_does_not_execute_a_denied_tool(denied_gate):
    """Fails while the registry prefers the dynamic handler over the gate."""
    import desk.chat as chat

    ok, result = chat._execute_tool_via_orchestrator(TOOL, ARGS, {})

    assert denied_gate["asked"] >= 1, (
        "the typed-chat path executed a consequential tool without ever "
        "consulting the authorisation gate"
    )
    assert denied_gate["executed"] == 0, (
        f"a denied tool ran anyway on the typed-chat path; it returned {result!r}"
    )
