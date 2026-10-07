"""Two holes in the permission gates, found in the 2026-10 audit.

1. The voice/terminal gate accepted any reply *containing* "go", "ok" or
   "yes" -- so "no, don't go ahead" confirmed a destructive action.
2. The desktop installs desk.confirm._ui_safety_gate in place of
   nova_safety.safety_gate, and that replacement dropped the taint branch.
   With a scope set to Allow, an action a web page asked for ran unasked.
"""
from __future__ import annotations

import pytest

import nova_safety
from nova_core import trust as _trust
from nova_core.permissions import Trust


@pytest.mark.parametrize("reply", [
    "no, don't go ahead", "no", "nope", "don't", "do not proceed",
    "wait", "stop", "cancel that", "not ok", "I'm not sure", "",
    "look at the broken token",          # contains "ok" inside words
    "go away",                            # "go" alone is not consent
])
def test_these_are_not_a_yes(reply):
    assert nova_safety.is_affirmative(reply) is False


@pytest.mark.parametrize("reply", [
    "yes", "Yes please", "yeah go ahead", "sure", "ok", "okay do it",
    "proceed", "confirmed", "go for it",
])
def test_these_are_a_yes(reply):
    assert nova_safety.is_affirmative(reply) is True


def test_the_voice_gate_refuses_on_a_negated_go():
    verdict = nova_safety._ask_permission(
        "file_controller", {"action": "delete", "path": "x"},
        lambda: "no, don't go ahead", speak_fn=lambda s: None)
    assert verdict is not None and "cancelled" in verdict.lower()


def test_trust_confirmation_shares_the_same_rule():
    from trust.confirmation import parse_confirmation
    assert parse_confirmation("no, don't go ahead") is False
    assert parse_confirmation("yes") is True


@pytest.fixture
def ui_gate(monkeypatch):
    from desk import confirm
    asked = []

    def fake_ask(tool, args, prompt):
        asked.append(tool)
        return False, "declined in test"

    monkeypatch.setattr(confirm.store, "ask", fake_ask)
    monkeypatch.setattr(confirm, "_permission_for", lambda t, a=None: "allow")
    return confirm, asked


def test_allow_still_runs_unasked_in_an_ordinary_turn(ui_gate):
    confirm, asked = ui_gate
    assert confirm._ui_safety_gate("open_app", {"app_name": "notepad"}) is None
    assert asked == []


def test_allow_does_not_cover_what_a_web_page_asked_for(ui_gate):
    confirm, asked = ui_gate
    with _trust.untrusted("web page https://evil.example"):
        verdict = confirm._ui_safety_gate("open_app", {"app_name": "notepad"})
    assert asked == ["open_app"], "the person was never asked"
    assert verdict is not None and "cancelled" in verdict.lower()
