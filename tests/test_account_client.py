"""End-to-end: the NOVA desktop client against a running backend.

A real server on a real socket, real HTTP, a real SQLite database. The point of
these tests is the behaviour that only appears when the two halves meet:
session persistence across restarts, two devices on one account, and what
happens when the network disappears mid-session.
"""
from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
import uuid

import pytest

PASSWORD = "a-long-enough-passphrase"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def server(monkeypatch):
    """A live NOVA Cloud on localhost."""
    tmp = tempfile.mkdtemp()
    os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tmp, "srv.db")
    os.environ["NOVA_ENV"] = "test"
    os.environ["NOVA_SECRET_KEY"] = "user-" + uuid.uuid4().hex
    os.environ["NOVA_ADMIN_SECRET_KEY"] = "admin-" + uuid.uuid4().hex
    os.environ["NOVA_ADMIN_REQUIRE_MFA"] = "0"

    from nova_cloud import config as cfgmod, db
    from nova_cloud.auth_guard import invalidate_auth_cache
    cfgmod.reset_config()
    db.reset_engine()
    invalidate_auth_cache()

    from werkzeug.serving import make_server
    from nova_cloud.app import create_app

    app = create_app()
    port = _free_port()
    srv = make_server("127.0.0.1", port, app, threaded=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    base = f"http://127.0.0.1:{port}"
    for _ in range(50):                       # wait for the socket to accept
        try:
            import requests
            requests.get(base + "/health", timeout=0.5)
            break
        except Exception:
            time.sleep(0.05)

    yield base

    srv.shutdown()
    db.reset_engine()
    cfgmod.reset_config()


@pytest.fixture()
def store_isolation(monkeypatch, tmp_path):
    """Keep tests out of the developer's real credential store."""
    import nova_secure_store as st
    fake: dict = {}
    monkeypatch.setattr(st, "set_secret", lambda k, v: fake.__setitem__(k, v))
    monkeypatch.setattr(st, "get_secret", lambda k: fake.get(k))
    monkeypatch.setattr(st, "delete_secret", lambda k: fake.pop(k, None))
    monkeypatch.setattr(st, "backend", lambda: "test")
    monkeypatch.setattr(st, "is_hardware_backed", lambda: False)
    return fake


def client_for(base):
    """A fresh NovaAccount reading whatever is in the (isolated) store."""
    import nova_account
    return nova_account.NovaAccount(base_url=base)


def as_new_machine(store_state: dict) -> None:
    """Wipe the installation identity, so the next client looks like a
    different computer rather than the same one."""
    for k in ("installation_identity", "device_identities", "account_session"):
        store_state.pop(k, None)


# -- lifecycle ---------------------------------------------------------------

def test_signup_then_status(server, store_isolation):
    a = client_for(server)
    st = a.sign_up("sam@example.com", PASSWORD, display_name="Sam")
    assert st["signed_in"] is True
    assert st["user"]["email"] == "sam@example.com"
    assert a.display_name == "Sam"


def test_nova_knows_the_user_name_without_asking(server, store_isolation):
    a = client_for(server)
    a.sign_up("sam@example.com", PASSWORD, display_name="Samuel")
    assert a.display_name == "Samuel"


def test_session_survives_a_restart(server, store_isolation):
    a = client_for(server)
    a.sign_up("sam@example.com", PASSWORD, display_name="Sam")

    # A brand new client object, as if NOVA had been closed and reopened.
    b = client_for(server)
    assert b.signed_in is True
    assert b.user["email"] == "sam@example.com"
    assert b.ensure_access_token(), "could not restore a usable token"


def test_sign_out_clears_the_session(server, store_isolation):
    a = client_for(server)
    a.sign_up("sam@example.com", PASSWORD)
    a.sign_out()
    assert a.signed_in is False
    assert client_for(server).signed_in is False


def test_wrong_password_raises_a_useful_error(server, store_isolation):
    import nova_account
    a = client_for(server)
    a.sign_up("sam@example.com", PASSWORD)
    a.sign_out()
    b = client_for(server)
    with pytest.raises(nova_account.AccountError) as e:
        b.sign_in("sam@example.com", "not-the-password")
    assert e.value.status == 401


# -- multi-device ------------------------------------------------------------

def test_two_devices_one_account(server, store_isolation, monkeypatch):
    """Device A creates the account; device B signs in and is recognised."""
    import nova_account

    a = client_for(server)
    a.sign_up("sam@example.com", PASSWORD, display_name="Sam")
    device_a = a.status()["device_id"]

    # Device B: a different installation, so a different stored identity.
    as_new_machine(store_isolation)
    b = client_for(server)
    b.sign_in("sam@example.com", PASSWORD)
    device_b = b.status()["device_id"]

    assert device_a != device_b, "two installations must not share an identity"
    assert b.user["id"] == a.user["id"], "same account expected"

    listed = {d["id"] for d in b.devices()}
    assert device_a in listed and device_b in listed


def test_preferences_reach_the_second_device(server, store_isolation):
    a = client_for(server)
    a.sign_up("sam@example.com", PASSWORD, display_name="Sam")
    a.push_preferences({"theme": "light", "response_style": "concise"})

    as_new_machine(store_isolation)
    b = client_for(server)
    b.sign_in("sam@example.com", PASSWORD)

    prefs = b.pull_preferences()
    assert prefs["theme"]["value"] == "light"
    assert prefs["response_style"]["value"] == "concise"


def test_a_change_on_b_flows_back_to_a(server, store_isolation):
    a = client_for(server)
    a.sign_up("sam@example.com", PASSWORD)
    a_state = dict(store_isolation)

    as_new_machine(store_isolation)
    b = client_for(server)
    b.sign_in("sam@example.com", PASSWORD)
    b.push_preferences({"theme": "dark"})

    store_isolation.clear()
    store_isolation.update(a_state)
    a2 = client_for(server)
    assert a2.pull_preferences()["theme"]["value"] == "dark"


def test_device_local_settings_are_refused_by_sync(server, store_isolation):
    """A microphone choice must not follow the user to another machine."""
    a = client_for(server)
    a.sign_up("sam@example.com", PASSWORD)
    r = a.push_preferences({"theme": "dark", "input_device": "USB Mic",
                            "zim_path": "C:/data/wiki.zim"})
    assert "theme" in r["applied"]
    assert set(r["rejected"]) == {"input_device", "zim_path"}


def test_last_write_wins_and_reports_conflicts(server, store_isolation):
    a = client_for(server)
    a.sign_up("sam@example.com", PASSWORD)
    now = time.time()
    a.push_preferences({"theme": {"value": "dark", "updated_at": now}})
    # An older write must not clobber the newer value.
    r = a.push_preferences({"theme": {"value": "light", "updated_at": now - 60}})
    assert r["conflicts"] and r["conflicts"][0]["server_value"] == "dark"
    assert a.pull_preferences()["theme"]["value"] == "dark"


def test_revoking_a_device_signs_it_out(server, store_isolation):
    """The lost-laptop flow, end to end through the client."""
    a = client_for(server)
    a.sign_up("sam@example.com", PASSWORD)
    a_state = dict(store_isolation)

    as_new_machine(store_isolation)
    lost = client_for(server)
    lost.sign_in("sam@example.com", PASSWORD)
    lost_id = lost.status()["device_id"]
    lost_state = dict(store_isolation)

    store_isolation.clear()
    store_isolation.update(a_state)
    a2 = client_for(server)
    a2.revoke_device(lost_id)

    store_isolation.clear()
    store_isolation.update(lost_state)
    thief = client_for(server)
    # Force a refresh: the cached access token is deliberately short-lived.
    thief._session["access_expires"] = 0
    assert thief.ensure_access_token() == ""
    assert thief.signed_in is False, "a revoked device kept its session"


# -- offline -----------------------------------------------------------------

def test_offline_does_not_sign_the_user_out(server, store_isolation):
    """The network dropping is not a logout."""
    a = client_for(server)
    a.sign_up("sam@example.com", PASSWORD, display_name="Sam")

    a.base = "http://127.0.0.1:1"          # nothing listening
    a._session["access_expires"] = 0       # force a refresh attempt

    assert a.ensure_access_token() == ""   # cannot refresh, as expected
    assert a.signed_in is True, "offline must not clear the session"
    assert a.display_name == "Sam", "cached identity must survive"
    assert a.status()["online"] is False


def test_offline_raises_a_typed_error_not_a_crash(server, store_isolation):
    import nova_account
    a = client_for(server)
    a.sign_up("sam@example.com", PASSWORD)
    a.base = "http://127.0.0.1:1"
    with pytest.raises(nova_account.Offline):
        a.devices()


def test_reconnecting_restores_the_session(server, store_isolation):
    a = client_for(server)
    a.sign_up("sam@example.com", PASSWORD)
    real = a.base

    a.base = "http://127.0.0.1:1"
    a._session["access_expires"] = 0
    assert a.ensure_access_token() == ""

    a.base = real
    assert a.ensure_access_token(), "did not recover when the network returned"
    assert a.online is True


def test_grace_period_eventually_expires(server, store_isolation):
    a = client_for(server)
    a.sign_up("sam@example.com", PASSWORD)
    # Simulate a device that has been offline for longer than the policy.
    a._session["offline_grace_s"] = 60
    a._session["last_verified"] = time.time() - 3600
    assert a.signed_in is False
    assert a.status()["grace_expired"] is True


def test_telemetry_never_blocks(server, store_isolation):
    """emit() must return immediately even with the backend unreachable."""
    a = client_for(server)
    a.sign_up("sam@example.com", PASSWORD)
    a.base = "http://127.0.0.1:1"
    t0 = time.perf_counter()
    for _ in range(200):
        a.emit("NOVA_STARTED", platform="windows")
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.25, f"emit() blocked for {elapsed:.3f}s"


def test_telemetry_queue_drops_rather_than_growing(server, store_isolation):
    a = client_for(server)
    a.stop_background()
    for _ in range(5000):
        a.emit("NOVA_STARTED")
    assert a._tel_q.qsize() <= 500


def test_telemetry_reaches_the_backend(server, store_isolation):
    a = client_for(server)
    a.sign_up("sam@example.com", PASSWORD)
    for _ in range(60):                     # over the batch threshold
        a.emit("VOICE_SESSION_STARTED", platform="windows")

    deadline = time.time() + 20
    seen = 0
    while time.time() < deadline:
        from nova_cloud.db import session_scope
        from nova_cloud.models import ActivityEvent
        from sqlalchemy import func, select
        with session_scope() as s:
            seen = int(s.scalar(select(func.count()).select_from(ActivityEvent)
                                .where(ActivityEvent.type == "VOICE_SESSION_STARTED")) or 0)
        if seen:
            break
        time.sleep(0.5)
    assert seen > 0, "telemetry never arrived at the backend"


# -- account separation ------------------------------------------------------

def test_signing_in_as_someone_else_does_not_show_the_previous_user(server,
                                                                   store_isolation):
    a = client_for(server)
    a.sign_up("first@example.com", PASSWORD, display_name="First")
    a.push_preferences({"theme": "light"})
    a.sign_out()

    b = client_for(server)
    b.sign_up("second@example.com", PASSWORD, display_name="Second")
    assert b.display_name == "Second"
    assert b.pull_preferences() == {}, "previous account's data leaked"


def test_two_accounts_on_one_computer(server, store_isolation):
    """Two people share a machine. Neither may see the other's account, and
    neither can sign the other out."""
    a = client_for(server)
    a.sign_up("first@example.com", PASSWORD, display_name="First")
    a.push_preferences({"theme": "light"})
    device_first = a.status()["device_id"]
    a.sign_out()

    # Same installation -- the store keeps installation_identity.
    b = client_for(server)
    b.sign_up("second@example.com", PASSWORD, display_name="Second")
    device_second = b.status()["device_id"]

    assert device_first != device_second, \
        "two accounts on one machine must not share a device registration"
    assert b.display_name == "Second"
    assert b.pull_preferences() == {}

    # The second user cannot even see the first user's device.
    assert device_first not in {d["id"] for d in b.devices()}


def test_signing_back_in_reuses_the_same_device_registration(server, store_isolation):
    """Signing out and back in must not accumulate a new device every time."""
    a = client_for(server)
    a.sign_up("sam@example.com", PASSWORD)
    first = a.status()["device_id"]
    a.sign_out()

    b = client_for(server)
    b.sign_in("sam@example.com", PASSWORD)
    assert b.status()["device_id"] == first
    assert len(b.devices()) == 1
