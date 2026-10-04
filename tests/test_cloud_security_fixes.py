"""Security faults found in the 2026-09-28 audit, each pinned by a test.

1. An enrolled admin's MFA could be replaced with the password alone.
2. A signup that failed on the device still committed the account.
3. Rate limits trusted a client-supplied X-Forwarded-For.
4. Email verification was recorded but never enforced.
"""
from __future__ import annotations

import os
import tempfile
import uuid

import pytest

PASSWORD = "a-long-enough-passphrase"


def _make_client(monkeypatch, **env):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + os.path.join(tmp, "t.db"))
    monkeypatch.setenv("NOVA_ENV", "test")
    monkeypatch.setenv("NOVA_SECRET_KEY", "user-" + uuid.uuid4().hex)
    monkeypatch.setenv("NOVA_ADMIN_SECRET_KEY", "admin-" + uuid.uuid4().hex)
    monkeypatch.setenv("NOVA_ADMIN_REQUIRE_MFA", "1")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from nova_cloud import config as cfgmod, db
    from nova_cloud.auth_guard import invalidate_auth_cache
    cfgmod.reset_config()
    db.reset_engine()
    invalidate_auth_cache()
    from nova_cloud.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def client(monkeypatch):
    app = _make_client(monkeypatch)
    with app.test_client() as c:
        yield c
    from nova_cloud import config as cfgmod, db
    db.reset_engine()
    cfgmod.reset_config()


def device():
    return {"device_id": "dev-" + uuid.uuid4().hex[:12],
            "device_secret": "secret-" + uuid.uuid4().hex,
            "platform": "windows", "app_version": "1.0.0", "device_name": "PC"}


def auth(t):
    return {"Authorization": f"Bearer {t}"}


# -- 1. admin MFA ------------------------------------------------------------

def _seed_admin(email="root@nova.local"):
    from nova_cloud import security as sec
    from nova_cloud.db import session_scope
    from nova_cloud.models import AdminUser
    with session_scope() as s:
        s.add(AdminUser(email=email, password_hash=sec.hash_password(PASSWORD),
                        role="SUPER_ADMIN"))


def _enrol(client, email="root@nova.local"):
    import pyotp
    start = client.post("/admin/api/auth/mfa/enrol",
                        json={"email": email, "password": PASSWORD})
    secret = start.get_json()["secret"]
    client.post("/admin/api/auth/mfa/enrol",
                json={"email": email, "password": PASSWORD,
                      "totp": pyotp.TOTP(secret).now()})
    return secret


def test_an_enrolled_admins_mfa_cannot_be_replaced_with_the_password(client):
    import pyotp
    _seed_admin()
    real_secret = _enrol(client)

    hijack = client.post("/admin/api/auth/mfa/enrol",
                         json={"email": "root@nova.local", "password": PASSWORD})
    assert hijack.status_code == 409, hijack.get_json()
    assert "secret" not in (hijack.get_json() or {})

    # The real authenticator still signs in.
    r = client.post("/admin/api/auth/login",
                    json={"email": "root@nova.local", "password": PASSWORD,
                          "totp": pyotp.TOTP(real_secret).now()})
    assert r.status_code == 200, r.get_json()


# -- 2. orphaned account on a failed signup ----------------------------------

def test_a_signup_that_fails_on_the_device_leaves_no_account(client):
    bad = {"email": "kim@example.com", "password": PASSWORD,
           "device_id": "", "device_secret": ""}
    r = client.post("/v1/auth/signup", json=bad)
    assert r.status_code == 400, r.get_json()

    ok = client.post("/v1/auth/signup",
                     json={"email": "kim@example.com", "password": PASSWORD, **device()})
    assert ok.status_code == 201, ok.get_json()


# -- 3. spoofed X-Forwarded-For ----------------------------------------------

def test_a_spoofed_forwarded_for_does_not_escape_the_signup_limit(client):
    codes = []
    for i in range(7):
        r = client.post("/v1/auth/signup",
                        json={"email": f"u{i}@example.com", "password": PASSWORD, **device()},
                        headers={"X-Forwarded-For": f"10.0.0.{i}"})
        codes.append(r.status_code)
    assert 429 in codes, codes


def test_behind_one_trusted_proxy_the_proxy_appended_address_counts(monkeypatch):
    app = _make_client(monkeypatch, NOVA_TRUSTED_PROXY_HOPS="1")
    from nova_cloud.api_auth import _client_ip
    with app.test_request_context(
            "/", headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.9"},
            environ_base={"REMOTE_ADDR": "172.17.0.1"}):
        assert _client_ip() == "203.0.113.9"
    with app.test_request_context("/", environ_base={"REMOTE_ADDR": "172.17.0.1"}):
        assert _client_ip() == "172.17.0.1"


def test_with_no_trusted_proxy_the_header_is_ignored(client):
    from nova_cloud.api_auth import _client_ip
    with client.application.test_request_context(
            "/", headers={"X-Forwarded-For": "6.6.6.6"},
            environ_base={"REMOTE_ADDR": "198.51.100.4"}):
        assert _client_ip() == "198.51.100.4"


# -- 4. email verification enforced ------------------------------------------

@pytest.fixture()
def verifying_client(monkeypatch):
    app = _make_client(monkeypatch, NOVA_REQUIRE_EMAIL_VERIFICATION="1")
    with app.test_client() as c:
        yield c
    from nova_cloud import config as cfgmod, db
    db.reset_engine()
    cfgmod.reset_config()


def test_an_unverified_account_reaches_only_the_verification_routes(verifying_client):
    c = verifying_client
    r = c.post("/v1/auth/signup", json={"email": "lee@example.com",
                                         "password": PASSWORD, **device()})
    assert r.status_code == 201
    tok = r.get_json()["access_token"]

    blocked = c.get("/v1/sync/preferences", headers=auth(tok))
    assert blocked.status_code == 403
    assert blocked.get_json()["error"] == "email_unverified"

    me = c.get("/v1/auth/me", headers=auth(tok))
    assert me.status_code == 200
    assert me.get_json()["user"]["email_verified"] is False


def test_verifying_the_email_opens_the_account(verifying_client):
    c = verifying_client
    r = c.post("/v1/auth/signup", json={"email": "ann@example.com",
                                         "password": PASSWORD, **device()})
    tok = r.get_json()["access_token"]
    from nova_cloud.db import session_scope
    from nova_cloud.models import User
    from sqlalchemy import select
    with session_scope() as s:
        s.scalar(select(User).where(User.email == "ann@example.com")).email_verified = True
    from nova_cloud.auth_guard import invalidate_auth_cache
    invalidate_auth_cache()
    assert c.get("/v1/sync/preferences", headers=auth(tok)).status_code == 200
