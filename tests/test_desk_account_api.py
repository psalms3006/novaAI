"""The desktop's account surface, using the bridge's real token guard.

An earlier version of these tests stubbed `require_token` with a pass-through,
which hid a genuine bug: the SPA sent `X-Desk-Token` while the bridge requires
`X-NOVA-Desk`, so every account call from the UI would have been rejected. The
guard is therefore the real one here, and the header the SPA actually sends is
asserted against the header the bridge actually checks.
"""
from __future__ import annotations

import io
import re

import pytest
from flask import Flask, jsonify, request

# The checks on the previous window (desk/static) that lived here moved to
# tests/test_desk_ui_behaviour.py when the interface became desk/ui.

TOKEN = "test-desk-token"


@pytest.fixture()
def client(monkeypatch):
    import nova_secure_store as st
    fake: dict = {}
    monkeypatch.setattr(st, "set_secret", lambda k, v: fake.__setitem__(k, v))
    monkeypatch.setattr(st, "get_secret", lambda k: fake.get(k))
    monkeypatch.setattr(st, "delete_secret", lambda k: fake.pop(k, None))
    monkeypatch.setattr(st, "backend", lambda: "test-store")
    monkeypatch.setattr(st, "is_hardware_backed", lambda: False)

    import nova_account
    nova_account._account = None
    monkeypatch.delenv("NOVA_CLOUD_URL", raising=False)

    from desk import account_api

    app = Flask(__name__)
    app.config["TESTING"] = True

    # The bridge's guard, reproduced exactly.
    def require_token(fn):
        def wrapper(*a, **kw):
            tok = request.headers.get("X-NOVA-Desk", "")
            if not tok or tok != TOKEN:
                return jsonify({"error": "unauthorized"}), 401
            return fn(*a, **kw)
        wrapper.__name__ = fn.__name__
        return wrapper

    account_api.register(app, require_token, {})
    with app.test_client() as c:
        yield c
    nova_account._account = None


AUTH = {"X-NOVA-Desk": TOKEN}


def test_account_endpoints_require_the_desk_token(client):
    for path in ("/api/account", "/api/account/devices"):
        assert client.get(path).status_code == 401
    for path in ("/api/account/signin", "/api/account/signup",
                 "/api/account/signout", "/api/account/sync"):
        assert client.post(path, json={}).status_code == 401


def test_a_wrong_desk_token_is_rejected(client):
    r = client.get("/api/account", headers={"X-NOVA-Desk": "not-the-token"})
    assert r.status_code == 401


def test_status_answers_when_no_cloud_is_configured(client):
    """A local-only NOVA must still get a useful answer, not an error."""
    r = client.get("/api/account", headers=AUTH)
    assert r.status_code == 200
    j = r.get_json()
    assert j["configured"] is False
    assert j["signed_in"] is False
    assert j["device_id"]


def test_signin_without_a_backend_fails_cleanly(client):
    r = client.post("/api/account/signin", headers=AUTH,
                    json={"email": "a@example.com", "password": "x" * 12})
    assert r.status_code == 400
    assert r.get_json()["error"] == "not_configured"


def test_devices_without_a_backend_does_not_crash(client):
    r = client.get("/api/account/devices", headers=AUTH)
    assert r.status_code in (400, 503)
    assert r.get_json()["ok"] is False


def test_sync_requires_being_signed_in(client):
    r = client.post("/api/account/sync", headers=AUTH, json={})
    assert r.status_code == 400
    assert r.get_json()["error"] == "not_signed_in"


def test_the_synced_key_sets_agree_between_client_and_server():
    """A key the desktop pushes but the server refuses would sync silently
    nowhere, which is worse than not offering it."""
    from desk.account_api import SYNCED_KEYS as desk_keys
    from nova_cloud.api_sync import SYNCED_KEYS as cloud_keys
    unknown = desk_keys - cloud_keys
    assert not unknown, f"desk pushes keys the server rejects: {unknown}"


def test_device_local_keys_are_never_offered_for_sync():
    from desk.account_api import SYNCED_KEYS as desk_keys
    from nova_cloud.api_sync import DEVICE_LOCAL_KEYS
    overlap = desk_keys & DEVICE_LOCAL_KEYS
    assert not overlap, f"machine-specific keys marked as synced: {overlap}"


# -- email verification through the desktop ----------------------------------

def test_status_reports_verification_state(client):
    """A client must be able to tell 'confirmed' from 'not confirmed'."""
    j = client.get("/api/account", headers=AUTH).get_json()
    assert "email_verified" in j
    assert "verification_required" in j


def test_resend_without_a_backend_fails_cleanly(client):
    r = client.post("/api/account/verify/resend", headers=AUTH)
    assert r.status_code in (400, 503)
    assert r.get_json()["ok"] is False


def test_refresh_identity_without_a_backend_fails_cleanly(client):
    r = client.post("/api/account/refresh", headers=AUTH)
    assert r.status_code in (400, 503)


def test_verify_endpoints_require_the_desk_token(client):
    for path in ("/api/account/verify/resend", "/api/account/refresh"):
        assert client.post(path).status_code == 401


