"""Saving a permission must not deadlock NOVA's settings.

set_many() held the module lock and called get(), which takes the same
non-reentrant lock. The first permission change hung that request forever and
every later settings read -- voice, the safety gate's permission lookups --
blocked behind it. The old interface never saved permissions; the new
Permissions page does, which is how this surfaced.
"""
from __future__ import annotations

import importlib
import threading

import pytest


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    import desk.settings as mod
    return importlib.reload(mod)


def _finishes(fn, seconds=5.0):
    done = threading.Event()
    result = {}

    def run():
        result["value"] = fn()
        done.set()

    threading.Thread(target=run, daemon=True).start()
    return done.wait(seconds), result.get("value")


def test_saving_a_permission_returns(settings):
    ok, _ = _finishes(lambda: settings.set_many({"permissions": {"files": "allow"}}))
    assert ok, "set_many deadlocked on a permissions update"


def test_settings_still_work_after_a_permission_change(settings):
    assert _finishes(lambda: settings.set_many({"permissions": {"exec": "deny"}}))[0]
    ok, value = _finishes(lambda: settings.get("permissions"))
    assert ok, "a later read blocked"
    assert value["exec"] == "deny"


def test_permission_updates_merge_and_validate(settings):
    settings.set_many({"permissions": {"files": "allow"}})
    settings.set_many({"permissions": {"exec": "deny", "files": "bogus", "nope": "allow"}})
    perms = settings.all()["permissions"]
    assert perms["files"] == "allow", "an invalid value must not overwrite a valid one"
    assert perms["exec"] == "deny"
    assert "nope" not in perms
    assert perms["web"] == "allow" and perms["screen"] == "ask", "untouched categories keep their defaults"
