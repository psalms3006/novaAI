"""A cloud install that started without network gets the cloud back.

resolve() ran at startup and on user actions only. Booting offline set the
mode to "offline" (no Gemini key) and nothing ever asked again, so voice and
cloud chat stayed dead until NOVA was restarted -- even with the network
back for hours. recover_cloud_if_back() is called on the scheduler tick.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture()
def creds(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("NOVA_MACHINE_DIR", str(tmp_path / "NOVA"))
    monkeypatch.delenv("NOVA_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    # apply_runtime() imports nova to push the key into it; importing the real
    # module loads the repo's .env, which carries a developer key and would
    # turn "cloud" into "env". A user's install has no such file.
    import sys, types
    monkeypatch.setitem(sys.modules, "nova", types.ModuleType("nova"))
    from desk import creds as mod
    monkeypatch.setattr(mod, "apply_gateway_shim", lambda genai: None)
    monkeypatch.setattr(mod, "_register_router_gemini", lambda: None)
    monkeypatch.setattr(mod, "_last_status", {})
    net = {"up": False}

    class Cloud:
        active = True

        def get_session(self):
            if not net["up"]:
                raise ConnectionError("NOVA Cloud is unreachable")
            return "access-token"

    monkeypatch.setattr(mod, "current_cloud_client", lambda: Cloud())
    yield mod, net
    os.environ.pop("GEMINI_API_KEY", None)


def test_offline_boot_then_network_back_returns_to_the_cloud(creds):
    mod, net = creds
    st = mod.resolve()
    assert st["mode"] == "offline" and st["cloud_error"]
    assert not os.environ.get("GEMINI_API_KEY")

    assert mod.recover_cloud_if_back() is False      # still offline
    net["up"] = True
    assert mod.recover_cloud_if_back() is True
    assert mod._last_status["mode"] == "cloud"
    assert os.environ.get("GEMINI_API_KEY") == mod._SENTINEL
    assert mod.recover_cloud_if_back() is False      # nothing left to recover


def test_a_person_who_chose_offline_is_left_offline(creds):
    mod, net = creds
    mod.resolve()
    mod.set_many({"auth_mode": "offline"})
    net["up"] = True
    assert mod.recover_cloud_if_back() is False


def test_a_healthy_boot_does_not_re_resolve(creds, monkeypatch):
    mod, net = creds
    net["up"] = True
    mod.resolve()
    called = []
    monkeypatch.setattr(mod, "apply_runtime", lambda: called.append(1) or {})
    assert mod.recover_cloud_if_back() is False and called == []
