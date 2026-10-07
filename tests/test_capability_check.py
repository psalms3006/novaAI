"""NOVA can say what she can't do, and why, from live state.

"Why can't you do that?" used to be answered from the model's imagination.
capability_check reads connectivity, model access, installed tools,
permission choices and the hardware, and sorts a task's needs into can't /
shouldn't / not yet. Offline, a network-only tool now fails immediately with
the reason instead of after a timeout.
"""
from __future__ import annotations

import sys
import types

import pytest

import actions.capability_check as cc


class _Conn:
    def __init__(self, value):
        self.state = types.SimpleNamespace(value=value)


@pytest.fixture
def machine(monkeypatch):
    fake = types.ModuleType("nova")
    fake._connectivity = _Conn("online")
    fake._TOOL_AVAILABILITY = {"web_search": True, "file_controller": True,
                               "email_search": False}
    fake._tool_available = lambda name: fake._TOOL_AVAILABILITY.get(name, False)
    fake._nova_router = None
    monkeypatch.setitem(sys.modules, "nova", fake)
    monkeypatch.setattr(cc, "_permissions", lambda: {"computer_control": "deny",
                                                      "file_read": "allow"})
    monkeypatch.setattr(cc, "hardware", lambda: {
        "cpus": 8, "ram_gb": 7.9, "ram_free_gb": 1.2, "disk_free_gb": 13.5,
        "nvidia_gpu": False, "gpus": ["Intel(R) UHD Graphics 620"]})
    return fake


def test_offline_says_what_is_unavailable_and_what_still_works(machine):
    machine._connectivity = _Conn("offline")
    out = cc.execute({"task": "search the web for the latest news"})
    assert "Network: offline" in out
    assert "Unavailable while offline" in out and "web_search" in out
    assert "CAN'T right now" in out and "Local files" in out


def test_training_without_a_cuda_gpu_is_a_cant_with_the_hardware_named(machine):
    out = cc.execute({"task": "fine-tune this model on my dataset"})
    assert "CAN'T train on a GPU" in out
    assert "Intel(R) UHD Graphics 620" in out and "7.9" in out


def test_a_permission_turned_off_is_a_shouldnt(machine):
    out = cc.execute({"task": "open spotify and turn the volume up"})
    assert "computer_control: SHOULDN'T" in out
    assert "Turned off in Settings: computer_control" in out


def test_a_missing_connector_is_a_not_yet(machine):
    out = cc.execute({"task": "check my email for the invoice"})
    assert "email: NOT YET" in out
    assert "Not installed: email_search" in out


def test_plain_conversation_needs_nothing(machine):
    out = cc.execute({"task": "tell me a joke"})
    assert "Nothing in the request needs a capability" in out


def test_offline_web_tool_fails_fast_through_the_real_dispatcher(monkeypatch):
    import nova
    monkeypatch.setattr(cc, "_connectivity", lambda: "offline")
    called = []
    import actions.web_search as ws
    monkeypatch.setattr(ws, "execute", lambda args: called.append(args) or "results")
    out = nova._execute_tool_sync("web_search", {"query": "weather"}, {})
    assert out.startswith("Can't do this right now")
    assert "Nothing was attempted" in out
    assert called == [], "the search ran anyway"


def test_capability_check_is_declared_and_ungated():
    import nova
    assert "capability_check" in {d["name"] for d in nova.TOOL_DECLARATIONS}
    from desk import confirm
    assert confirm._permission_for("capability_check", {}) == "allow"


def test_the_prompt_asks_for_a_recommendation_and_the_three_kinds_of_no():
    import nova_personality as p
    core = p._CORE
    assert "Give a recommendation, not a neutral list" in core
    assert "can't" in core and "shouldn't" in core and "not yet" in core
    assert "capability_check" in core
