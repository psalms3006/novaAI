"""Two safety faults from the 2026-09-30 session log.

21:52:27 computer_settings({"action": "shutdown"}) -> "Shutdown scheduled in 0 seconds."
  * it was auto-approved because "Control the computer" was set to Allow;
  * the model had withdrawn the call four seconds earlier, and it ran anyway.
"""
import threading
import time

import pytest

from desk import confirm


@pytest.fixture
def allow_all(monkeypatch):
    monkeypatch.setattr(confirm, "_permission_for", lambda t, a=None: "allow")
    asked = []

    def ask(tool, args, prompt, timeout=None):
        asked.append((tool, args.get("action")))
        return ask.answer, ""
    ask.answer = False
    monkeypatch.setattr(confirm.store, "ask", ask)
    return asked, ask


@pytest.mark.parametrize("tool, action", [
    ("computer_settings", "shutdown"), ("computer_settings", "restart"), ("computer_settings", "sleep"),
    ("computer_settings", "logoff"), ("computer_control", "shutdown"), ("file_controller", "delete"),
    ("computer_settings", "uninstall_app"),
])
def test_irreversible_actions_ask_even_when_allowed(allow_all, tool, action):
    asked, ask = allow_all
    out = confirm._ui_safety_gate(tool, {"action": action})
    assert asked == [(tool, action)] and out and "cancelled" in out.lower()
    ask.answer = True
    assert confirm._ui_safety_gate(tool, {"action": action}) is None


@pytest.mark.parametrize("tool, action", [
    ("computer_settings", "volume_up"), ("computer_settings", "screenshot"), ("file_controller", "list"),
    ("file_controller", "copy"),
])
def test_reversible_actions_still_run_unasked_on_allow(allow_all, tool, action):
    asked, _ = allow_all
    assert confirm._ui_safety_gate(tool, {"action": action}) is None and asked == []


def test_never_still_refuses(monkeypatch):
    monkeypatch.setattr(confirm, "_permission_for", lambda t, a=None: "deny")
    assert "turned off" in confirm._ui_safety_gate("computer_settings", {"action": "shutdown"})


def test_a_withdrawn_call_does_not_run(monkeypatch):
    import nova
    from nova_core import cancel
    ran = []
    monkeypatch.setattr(nova, "_execute_learn_resource", lambda a: ran.append(1) or "ran")
    ev = threading.Event()
    ev.set()
    try:
        out = nova._execute_tool_core("learn_resource", {"path": "x"}, {"_cancel": ev})
    finally:
        cancel.set_current(None)
    assert out == cancel.WITHDRAWN and ran == []


def test_withdrawing_during_a_confirmation_closes_it():
    from nova_core import cancel
    store = confirm.ConfirmationStore()
    ev = threading.Event()
    result = {}

    def worker():
        cancel.set_current(ev)
        result["r"] = store.ask("computer_settings", {"action": "shutdown"}, "Shut down?", timeout=10)
    t = threading.Thread(target=worker)
    t0 = time.time()
    t.start()
    time.sleep(0.3)
    assert store.pending()                      # the question is showing
    ev.set()                                    # the model withdraws the call
    t.join(3)
    assert result["r"][0] is False and "withdrawn" in result["r"][1]
    assert time.time() - t0 < 2 and store.pending() == []


def test_the_live_session_signals_withdrawn_calls():
    from desk import live_session as ls
    m = ls.LiveManager()
    m._cancel_events = {"fc_1": threading.Event(), "fc_2": threading.Event()}
    m._cancel_tool_calls(["fc_1"])
    assert m._cancel_events["fc_1"].is_set() and not m._cancel_events["fc_2"].is_set()


def test_camera_and_saved_pictures_get_a_realistic_budget():
    from desk import live_session as ls
    assert ls.VISION_CORE_TIMEOUT_S >= 25 > ls.VISION_TIMEOUT_S
