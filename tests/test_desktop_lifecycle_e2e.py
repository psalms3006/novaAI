"""The desktop's first-run lifecycle against a real NOVA backend.

A real nova_cloud server runs on a local port with a temporary database; the
desktop's account endpoints talk to it over HTTP exactly as in production.
Only the OS credential store is replaced (a dict), so nothing touches the
developer's own Windows Credential Manager.

Covers the product's lifecycle tests that can run without a GUI:
  B  close and reopen          -> no onboarding
  D  sign out                  -> sign-in screen, not onboarding
  E  sign in again             -> same instance, same data, no onboarding
  I  a second person           -> own instance, nothing leaks
  J  same person, second PC    -> profile skipped, device steps asked
"""
from __future__ import annotations

import os
import socket
import tempfile
import threading
import uuid

import pytest
from flask import Flask

PASSWORD = "a-long-enough-passphrase"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def backend():
    tmp = tempfile.mkdtemp()
    env = {"DATABASE_URL": "sqlite:///" + os.path.join(tmp, "cloud.db"), "NOVA_ENV": "test",
           "NOVA_SECRET_KEY": "u-" + uuid.uuid4().hex,
           "NOVA_ADMIN_SECRET_KEY": "a-" + uuid.uuid4().hex,
           "NOVA_REQUIRE_EMAIL_VERIFICATION": "0"}
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    from nova_cloud import config as cfgmod, db
    cfgmod.reset_config()
    db.reset_engine()
    from nova_cloud.app import create_app
    from werkzeug.serving import make_server
    port = _free_port()
    srv = make_server("127.0.0.1", port, create_app(), threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()
    db.reset_engine()
    cfgmod.reset_config()
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class Desktop:
    """One installation of NOVA: its own machine folder and credential store."""

    def __init__(self, backend_url: str, root, monkeypatch):
        self.root = root
        self.store: dict = {}
        self.brain_starts = 0
        self.url = backend_url
        self.mp = monkeypatch
        self._boot()

    def _boot(self):
        """Equivalent to launching NOVA.exe on this PC."""
        mp = self.mp
        mp.setenv("APPDATA", str(self.root))
        mp.setenv("NOVA_MACHINE_DIR", str(self.root / "NOVA"))
        mp.setenv("NOVA_CLOUD_URL", self.url)
        mp.delenv("NOVA_ACCOUNT_ID", raising=False)
        import nova_secure_store as st
        mp.setattr(st, "set_secret", lambda k, v: self.store.__setitem__(k, v))
        mp.setattr(st, "get_secret", lambda k: self.store.get(k))
        mp.setattr(st, "delete_secret", lambda k: self.store.pop(k, None))
        mp.setattr(st, "backend", lambda: "test-store")
        mp.setattr(st, "is_hardware_backed", lambda: False)
        import nova_account
        nova_account._account = None
        import nova_lifecycle
        import nova_runtime
        mp.setattr(nova_runtime, "start_brain", self._start_brain)
        from desk import account_api
        mp.setattr(account_api, "_NO_RELAUNCH", True)

        # What nova_desktop_app.main() does before opening the window.
        first = nova_lifecycle.is_first_run()
        nova_lifecycle.ensure_installation("1.0.0")
        mp.setenv("NOVA_FIRST_RUN", "1" if first else "0")
        acct = nova_account.account()
        if acct.signed_in:
            nova_lifecycle.activate_account(acct.user_id)
            self._start_brain()

        app = Flask(__name__)
        app.config["TESTING"] = True
        account_api.register(app, lambda fn: fn, {})
        self.http = app.test_client()

    def _start_brain(self):
        self.brain_starts += 1
        return True

    def restart(self):
        self._boot()

    def lifecycle(self) -> dict:
        return self.http.get("/api/lifecycle").get_json()

    def sign_up(self, email):
        r = self.http.post("/api/account/signup",
                           json={"email": email, "password": PASSWORD, "display_name": "Sam"})
        assert r.status_code == 200, r.get_json()
        return r.get_json()

    def sign_in(self, email):
        r = self.http.post("/api/account/signin", json={"email": email, "password": PASSWORD})
        assert r.status_code == 200, r.get_json()
        return r.get_json()

    def profile(self, **fields):
        r = self.http.post("/api/lifecycle/profile", json=fields)
        assert r.status_code == 200, r.get_json()
        return r.get_json()

    def finish(self, **body):
        r = self.http.post("/api/lifecycle/complete", json=body)
        assert r.status_code == 200, r.get_json()
        return r.get_json()


def test_the_whole_lifecycle(backend, tmp_path, monkeypatch):
    pc = Desktop(backend, tmp_path / "pc1", monkeypatch)

    # A: first launch goes to sign-in, and the brain has not started.
    first = pc.lifecycle()
    assert first["first_run"] is True and first["next"] == "sign_in"
    assert pc.brain_starts == 0

    pc.sign_up("psalms@example.com")
    assert pc.brain_starts == 1
    assert pc.lifecycle()["next"] == "profile"

    pc.profile(preferred_name="Psalms", role="developer", about="I build NOVA.")
    assert pc.lifecycle()["next"] == "device_setup"

    done = pc.finish(permissions={"microphone": "allow", "file_write": "deny"},
                     startup_mode="manual", offline_model="skipped")
    assert done["next"] == "none"
    labels = [s["id"] for s in done["steps"]]
    assert labels[:1] == ["account"] and "permissions" in labels and "offline_model" in labels
    off = next(s for s in done["steps"] if s["id"] == "offline_model")
    assert off["ok"] is False and "later" in off["detail"]

    from desk import settings
    assert settings.get("user_name") == "Psalms"
    assert settings.get("permissions")["file_write"] == "deny"
    from desk import store
    cid = store.new_conversation("Psalms' first chat")

    # B: close and reopen -> straight into NOVA, brain starts with no sign-in.
    pc.restart()
    again = pc.lifecycle()
    assert again["first_run"] is False and again["next"] == "none"
    assert pc.brain_starts == 2

    # Update: a new version launching changes nothing.
    import nova_lifecycle
    nova_lifecycle.ensure_installation("1.1.0")
    assert pc.lifecycle()["next"] == "none"

    # D: sign out -> sign-in screen, never onboarding.
    out = pc.http.post("/api/account/signout", json={}).get_json()
    assert out["restarting"] is True
    pc.restart()
    signed_out = pc.lifecycle()
    assert signed_out["next"] == "sign_in" and signed_out["first_run"] is False

    # E: sign in again -> same instance, same data, no onboarding.
    pc.sign_in("psalms@example.com")
    back = pc.lifecycle()
    assert back["next"] == "none"
    assert settings.get("user_name") == "Psalms"
    assert any(c["id"] == cid for c in store.list_conversations())

    # I: a second person on the same PC -> their own instance and data.
    pc.http.post("/api/account/signout", json={})
    pc.restart()
    pc.sign_up("guest@example.com")
    other = pc.lifecycle()
    assert other["next"] == "profile"
    assert other["instance"]["id"] != back["instance"]["id"]
    assert settings.get("user_name") != "Psalms"
    assert all(c["id"] != cid for c in store.list_conversations())
    assert settings.get("permissions")["file_write"] == "ask"      # defaults, not Psalms'

    # J: the first person on a second PC -> profile skipped, device steps asked.
    laptop = Desktop(backend, tmp_path / "pc2", monkeypatch)
    laptop.sign_in("psalms@example.com")
    second = laptop.lifecycle()
    assert second["next"] == "device_setup"
    assert second["instance"]["id"] == back["instance"]["id"]


def test_profile_fields_can_all_be_skipped(backend, tmp_path, monkeypatch):
    pc = Desktop(backend, tmp_path / "pc", monkeypatch)
    pc.sign_up("skipper@example.com")
    pc.profile()
    assert pc.lifecycle()["next"] == "device_setup"


def test_a_session_the_server_rejects_returns_to_sign_in_not_onboarding(backend, tmp_path, monkeypatch):
    pc = Desktop(backend, tmp_path / "pc", monkeypatch)
    pc.sign_up("revoked@example.com")
    pc.profile()
    pc.finish(startup_mode="manual")
    # Everything this device holds stops working (e.g. revoked elsewhere).
    import nova_account
    acct = nova_account.account()
    acct._session["access_token"] = ""
    acct._session["access_expires"] = 0
    acct._session["refresh_token"] = "not-a-real-refresh-token"
    assert acct.ensure_access_token() == ""
    pc.restart()
    st = pc.lifecycle()
    assert st["next"] == "sign_in" and st["first_run"] is False
