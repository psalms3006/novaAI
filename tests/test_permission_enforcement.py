"""Every permission choice changes what NOVA can actually do (§117-120).

Found by the interaction audit: microphone "Ask me" behaved exactly like
"Allow", screen "Never" did not stop screen streaming, a withdrawn permission
only applied to the next session, in-session vision skipped the gate, and
several tools ran unasked because no scope mapped to them.
"""
import asyncio
import types

import pytest


class FakeLive:
    def __init__(self):
        self.started = self.stopped = 0
        self.watching = False
        self.owns_microphone = False

    def start(self):
        self.started += 1
        self.owns_microphone = True
        return {"ok": True, "message": "connecting"}

    def stop(self):
        self.stopped += 1
        self.owns_microphone = False
        return {"ok": True}

    def screen_status(self):
        return {"watching": self.watching}

    def set_screen_share(self, on):
        self.watching = bool(on)
        return {"ok": True, "watching": self.watching}


@pytest.fixture
def client(monkeypatch):
    from desk import bridge
    live = FakeLive()
    monkeypatch.setattr(bridge.desk_live, "get_live_manager", lambda: live)
    monkeypatch.setattr(bridge, "run_token", "t")
    perms = {}
    real_get = bridge.desk_settings.get
    monkeypatch.setattr(bridge.desk_settings, "get",
                        lambda k, d=None: dict(perms) if k == "permissions" else real_get(k, d))
    c = bridge.app.test_client()
    c.live, c.perms = live, perms
    return c


H = {"X-NOVA-Desk": "t"}


def test_microphone_never_refuses_voice_and_push_to_talk(client):
    client.perms["microphone"] = "deny"
    r = client.post("/api/live/start", json={"source": "user"}, headers=H)
    assert r.status_code == 403 and "turned off" in r.get_json()["message"]
    assert client.post("/api/voice/start", headers=H).status_code == 403
    assert client.live.started == 0


def test_microphone_ask_waits_for_the_person(client):
    client.perms["microphone"] = "ask"
    j = client.post("/api/live/start", json={"source": "auto"}, headers=H).get_json()
    assert j["error"] == "microphone_ask" and client.live.started == 0
    assert client.post("/api/live/start", json={"source": "user"}, headers=H).get_json()["ok"]
    assert client.live.started == 1


def test_microphone_allow_starts_on_its_own(client):
    client.perms["microphone"] = "allow"
    assert client.post("/api/live/start", json={"source": "auto"}, headers=H).get_json()["ok"]


def test_screen_never_and_ask_are_real(client):
    client.perms["screen_read"] = "deny"
    assert client.post("/api/live/screen", json={"watching": True}, headers=H).status_code == 403
    client.perms["screen_read"] = "ask"
    j = client.post("/api/live/screen", json={"watching": True, "source": "ambient"}, headers=H).get_json()
    assert j["ok"] is False and client.live.watching is False       # ambient did not ask
    j = client.post("/api/live/screen", json={"watching": True, "source": "user"}, headers=H).get_json()
    assert j["watching"] is True                                    # the person switched it on
    assert client.post("/api/live/screen", json={"watching": False}, headers=H).get_json()["watching"] is False


def test_withdrawing_a_permission_stops_what_it_allowed(client, monkeypatch):
    from desk import bridge
    monkeypatch.setattr(bridge.desk_settings, "set_many", lambda d: client.perms.update(d.get("permissions", {})))
    monkeypatch.setattr(bridge.desk_account, "push_preferences_async", lambda d: None)
    monkeypatch.setattr(bridge.desk_settings, "all", lambda: {"permissions": dict(client.perms)})
    client.perms.update(microphone="allow", screen_read="allow")
    client.post("/api/live/start", json={"source": "user"}, headers=H)
    client.live.watching = True
    client.post("/api/settings", json={"permissions": {"microphone": "deny", "screen_read": "deny"}}, headers=H)
    assert client.live.stopped == 1 and client.live.watching is False


def test_in_session_vision_obeys_the_screen_permission(monkeypatch):
    from desk import live_session as ls
    from desk import settings as desk_settings
    mgr = ls.LiveManager()
    fc = types.SimpleNamespace(id="1", name="vision", args={"angle": "screen"})
    monkeypatch.setattr(desk_settings, "get", lambda k, d=None: {"screen_read": "deny"} if k == "permissions" else d)
    r = asyncio.run(mgr._vision_permission(fc, {"angle": "screen"}))
    assert "turned off" in r.response["output"]
    monkeypatch.setattr(desk_settings, "get", lambda k, d=None: {"screen_read": "allow"} if k == "permissions" else d)
    assert asyncio.run(mgr._vision_permission(fc, {})) is None
    from desk.confirm import store
    monkeypatch.setattr(desk_settings, "get", lambda k, d=None: {"screen_read": "ask"} if k == "permissions" else d)
    monkeypatch.setattr(store, "ask", lambda *a, **k: (False, "no"))
    r = asyncio.run(mgr._vision_permission(fc, {"angle": "camera"}))
    assert "did not agree" in r.response["output"] and "camera" in r.response["output"]
    monkeypatch.setattr(store, "ask", lambda *a, **k: (True, ""))
    assert asyncio.run(mgr._vision_permission(fc, {})) is None


@pytest.mark.parametrize("tool, args, scope", [
    ("generate_document", {"format": "pdf"}, "file_write"),
    ("app_control", {"action": "click"}, "computer_control"),
    ("learn_resource", {"path": "x"}, "exec"),
    ("mcp__notion__create_page", {}, "network"),
    ("nova_capability", {"cmd": "run"}, "network"),
    ("nova_capability", {"cmd": "discover"}, "browser_read"),
    ("nova_capability", {"cmd": "list"}, ""),
    ("nova_learning", {"cmd": "learn"}, "file_read"),
    ("nova_learning", {"cmd": "status"}, ""),
])
def test_every_acting_tool_answers_to_a_permission(tool, args, scope):
    from desk.confirm import scope_for
    assert scope_for(tool, args) == scope


def test_the_window_is_told_when_a_change_was_not_saved(monkeypatch):
    """settings.set_many swallowed write errors, so the window said SAVED about
    changes that never reached the disk."""
    from desk import bridge
    from desk import settings as ds

    class Unwritable:
        def write_text(self, *a, **k):
            raise PermissionError("settings.json is read-only")
    monkeypatch.setattr(ds, "last_write_error", "")       # restored after the test
    monkeypatch.setattr(ds, "settings_path", lambda: Unwritable())
    monkeypatch.setattr(ds, "_load", lambda: {})
    monkeypatch.setattr(bridge, "run_token", "t")
    r = bridge.app.test_client().post("/api/settings", json={"response_style": "concise"}, headers=H)
    assert r.status_code == 500 and "could not be saved" in r.get_json()["error"]
    assert "read-only" in ds.last_write_error


def test_a_scoped_tool_set_to_never_is_refused(monkeypatch):
    from desk import confirm
    monkeypatch.setattr(confirm, "_permission_for", lambda t, a=None: "deny")
    out = confirm._ui_safety_gate("generate_document", {"format": "pdf"})
    assert out and "turned off" in out
