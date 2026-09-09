"""Security boundaries.

Every test here is an attack that must fail. They exist because these are the
failures that would matter: one user reading another's data, a normal user
reaching the admin plane, a revoked device still working, a client asserting
its own identity.
"""
from __future__ import annotations

import os
import tempfile
import uuid

import pytest

PASSWORD = "a-long-enough-passphrase"


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + os.path.join(tmp, "t.db"))
    monkeypatch.setenv("NOVA_ENV", "test")
    monkeypatch.setenv("NOVA_SECRET_KEY", "user-key-" + uuid.uuid4().hex)
    monkeypatch.setenv("NOVA_ADMIN_SECRET_KEY", "admin-key-" + uuid.uuid4().hex)
    monkeypatch.setenv("NOVA_ADMIN_REQUIRE_MFA", "0")

    from nova_cloud import config as cfgmod, db
    from nova_cloud.auth_guard import invalidate_auth_cache
    cfgmod.reset_config()
    db.reset_engine()
    invalidate_auth_cache()

    from nova_cloud.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
    db.reset_engine()
    cfgmod.reset_config()


def device(tag: str) -> dict:
    return {"device_id": f"dev-{tag}-{uuid.uuid4().hex[:8]}",
            "device_secret": "secret-" + uuid.uuid4().hex,
            "platform": "windows", "app_version": "0.1.0",
            "device_name": f"machine-{tag}"}


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def make_user(client, email: str, tag: str = "a"):
    r = client.post("/v1/auth/signup", json={
        "email": email, "password": PASSWORD, "display_name": email.split("@")[0],
        **device(tag)})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def make_admin(client, email="root@nova.local", role="SUPER_ADMIN"):
    from nova_cloud import security as sec
    from nova_cloud.db import session_scope
    from nova_cloud.models import AdminUser
    with session_scope() as s:
        s.add(AdminUser(email=email, password_hash=sec.hash_password(PASSWORD),
                        role=role, mfa_enabled=False))
    r = client.post("/admin/api/auth/login",
                    json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["access_token"]


# -- user isolation ----------------------------------------------------------

def test_user_a_cannot_read_user_b_devices(client):
    a = make_user(client, "a@example.com", "a")
    b = make_user(client, "b@example.com", "b")

    r = client.get("/v1/devices", headers=auth(a["access_token"]))
    ids = {d["id"] for d in r.get_json()["devices"]}
    assert b["device"]["id"] not in ids


def test_user_a_cannot_revoke_user_b_device(client):
    a = make_user(client, "a@example.com", "a")
    b = make_user(client, "b@example.com", "b")
    r = client.post(f"/v1/devices/{b['device']['id']}/revoke",
                    headers=auth(a["access_token"]))
    assert r.status_code == 404          # not even acknowledged as existing


def test_user_a_cannot_rename_user_b_device(client):
    a = make_user(client, "a@example.com", "a")
    b = make_user(client, "b@example.com", "b")
    r = client.patch(f"/v1/devices/{b['device']['id']}",
                     headers=auth(a["access_token"]), json={"name": "pwned"})
    assert r.status_code == 404


def test_preferences_are_scoped_to_the_account(client):
    a = make_user(client, "a@example.com", "a")
    b = make_user(client, "b@example.com", "b")
    client.post("/v1/sync/preferences", headers=auth(a["access_token"]),
                json={"preferences": {"theme": "light"}})
    r = client.get("/v1/sync/preferences", headers=auth(b["access_token"]))
    assert r.get_json()["preferences"] == {}


def test_a_client_cannot_assert_its_own_user_id(client):
    """Identity comes from the token, never the body."""
    a = make_user(client, "a@example.com", "a")
    b = make_user(client, "b@example.com", "b")
    client.post("/v1/sync/preferences", headers=auth(b["access_token"]),
                json={"preferences": {"theme": "dark"}})

    # A tries to write into B's account by claiming B's id in the payload.
    client.post("/v1/sync/preferences", headers=auth(a["access_token"]),
                json={"user_id": b["user"]["id"],
                      "preferences": {"theme": "hijacked"}})

    r = client.get("/v1/sync/preferences", headers=auth(b["access_token"]))
    assert r.get_json()["preferences"]["theme"]["value"] == "dark"


def test_a_tampered_token_is_rejected(client):
    a = make_user(client, "a@example.com", "a")
    tok = a["access_token"]
    head, payload, sig = tok.split(".")
    forged = f"{head}.{payload}.{'A' * len(sig)}"
    assert client.get("/v1/auth/me", headers=auth(forged)).status_code == 401


def test_a_token_signed_with_the_wrong_key_is_rejected(client):
    import jwt
    import time
    forged = jwt.encode({"sub": "someone", "did": "d", "sid": "s", "ep": 0,
                         "aud": "nova:user", "iat": int(time.time()),
                         "exp": int(time.time()) + 600},
                        "not-the-server-key", algorithm="HS256")
    assert client.get("/v1/auth/me", headers=auth(forged)).status_code == 401


def test_an_expired_token_is_rejected(client):
    import time
    import jwt
    from nova_cloud.config import config
    a = make_user(client, "a@example.com", "a")
    claims = jwt.decode(a["access_token"], config().secret_key,
                        algorithms=["HS256"], audience="nova:user")
    claims["exp"] = int(time.time()) - 10
    stale = jwt.encode(claims, config().secret_key, algorithm="HS256")
    r = client.get("/v1/auth/me", headers=auth(stale))
    assert r.status_code == 401
    assert r.get_json()["error"] == "token_expired"


# -- device revocation -------------------------------------------------------

def test_a_revoked_device_loses_access(client):
    """The lost-laptop case."""
    a = make_user(client, "a@example.com", "a")
    second = device("b")
    lost = client.post("/v1/auth/login", json={
        "email": "a@example.com", "password": PASSWORD, **second}).get_json()

    assert client.get("/v1/auth/me",
                      headers=auth(lost["access_token"])).status_code == 200

    r = client.post(f"/v1/devices/{lost['device']['id']}/revoke",
                    headers=auth(a["access_token"]))
    assert r.status_code == 200

    # The access token stops working...
    r2 = client.get("/v1/auth/me", headers=auth(lost["access_token"]))
    assert r2.status_code == 403
    assert r2.get_json()["error"] == "device_revoked"

    # ...and it cannot get a new one.
    r3 = client.post("/v1/auth/refresh",
                     json={"refresh_token": lost["refresh_token"]})
    assert r3.status_code == 403


def test_revoking_one_device_does_not_sign_out_the_others(client):
    a = make_user(client, "a@example.com", "a")
    other = client.post("/v1/auth/login", json={
        "email": "a@example.com", "password": PASSWORD, **device("b")}).get_json()
    client.post(f"/v1/devices/{other['device']['id']}/revoke",
                headers=auth(a["access_token"]))
    assert client.get("/v1/auth/me",
                      headers=auth(a["access_token"])).status_code == 200


# -- admin boundary ----------------------------------------------------------

ADMIN_ENDPOINTS = [
    ("get", "/admin/api/dashboard"),
    ("get", "/admin/api/users"),
    ("get", "/admin/api/devices"),
    ("get", "/admin/api/activity"),
    ("get", "/admin/api/models"),
    ("get", "/admin/api/agents"),
    ("get", "/admin/api/errors"),
    ("get", "/admin/api/health"),
    ("get", "/admin/api/flags"),
    ("get", "/admin/api/audit"),
    ("get", "/admin/api/admins"),
]


@pytest.mark.parametrize("method,path", ADMIN_ENDPOINTS)
def test_a_normal_user_token_cannot_reach_admin_endpoints(client, method, path):
    a = make_user(client, "a@example.com", "a")
    r = getattr(client, method)(path, headers=auth(a["access_token"]))
    assert r.status_code in (401, 403), f"{path} accepted a user token"


@pytest.mark.parametrize("method,path", ADMIN_ENDPOINTS)
def test_admin_endpoints_require_authentication(client, method, path):
    assert getattr(client, method)(path).status_code == 401


def test_an_admin_token_cannot_be_used_on_the_user_api(client):
    token = make_admin(client)
    assert client.get("/v1/auth/me", headers=auth(token)).status_code == 401


def test_role_permissions_are_enforced(client):
    """An analyst may read metrics but must not change an account."""
    make_user(client, "victim@example.com", "v")
    analyst = make_admin(client, "analyst@nova.local", role="ANALYST")

    assert client.get("/admin/api/dashboard",
                      headers=auth(analyst)).status_code == 200

    from nova_cloud.db import session_scope
    from nova_cloud.models import User
    from sqlalchemy import select
    with session_scope() as s:
        uid = s.scalar(select(User.id))

    r = client.post(f"/admin/api/users/{uid}/status",
                    headers=auth(analyst), json={"status": "disabled"})
    assert r.status_code == 403
    assert client.get("/admin/api/users", headers=auth(analyst)).status_code == 403


def test_support_cannot_create_admins(client):
    support = make_admin(client, "support@nova.local", role="SUPPORT")
    r = client.post("/admin/api/admins", headers=auth(support),
                    json={"email": "new@nova.local", "password": PASSWORD,
                          "role": "SUPER_ADMIN"})
    assert r.status_code == 403


def test_admin_logout_ends_the_session_immediately(client):
    token = make_admin(client)
    assert client.get("/admin/api/auth/me", headers=auth(token)).status_code == 200
    client.post("/admin/api/auth/logout", headers=auth(token))
    r = client.get("/admin/api/auth/me", headers=auth(token))
    assert r.status_code == 401, "a signed-out admin token still worked"


def test_disabling_a_user_cuts_their_access(client):
    a = make_user(client, "a@example.com", "a")
    token = make_admin(client)
    r = client.post(f"/admin/api/users/{a['user']['id']}/status",
                    headers=auth(token), json={"status": "disabled"})
    assert r.status_code == 200
    r2 = client.get("/v1/auth/me", headers=auth(a["access_token"]))
    assert r2.status_code == 403
    assert client.post("/v1/auth/refresh",
                       json={"refresh_token": a["refresh_token"]}).status_code == 403


def test_admin_cannot_read_user_content(client):
    """There must be no endpoint that returns private conversation data."""
    a = make_user(client, "a@example.com", "a")
    token = make_admin(client)
    r = client.get(f"/admin/api/users/{a['user']['id']}", headers=auth(token))
    assert r.status_code == 200
    blob = r.get_data(as_text=True).lower()
    for forbidden in ("transcript", "conversation_text", "message_body",
                      "password_hash", "prompt"):
        assert forbidden not in blob


def test_admin_actions_are_audited(client):
    a = make_user(client, "a@example.com", "a")
    token = make_admin(client)
    client.post(f"/admin/api/users/{a['user']['id']}/status",
                headers=auth(token), json={"status": "disabled"})
    entries = client.get("/admin/api/audit",
                         headers=auth(token)).get_json()["entries"]
    actions = {e["action"] for e in entries}
    assert "users.set_status" in actions
    assert "admin.login" in actions
    hit = next(e for e in entries if e["action"] == "users.set_status")
    assert hit["target_id"] == a["user"]["id"]
    assert hit["admin_email"] == "root@nova.local"


def test_a_failed_admin_login_is_audited(client):
    make_admin(client)
    client.post("/admin/api/auth/login",
                json={"email": "root@nova.local", "password": "wrong-password-x"})
    token = make_admin(client, "second@nova.local")
    entries = client.get("/admin/api/audit",
                         headers=auth(token)).get_json()["entries"]
    assert any(e["action"] == "admin.login" and e["result"] == "failure"
               for e in entries)


# -- rate limiting -----------------------------------------------------------

def test_login_is_rate_limited(client):
    make_user(client, "a@example.com", "a")
    codes = [client.post("/v1/auth/login",
                         json={"email": "a@example.com", "password": "bad-guess-x",
                               **device("z")}).status_code
             for _ in range(25)]
    assert 429 in codes, "brute force was not rate limited"


def test_signup_is_rate_limited(client):
    codes = [client.post("/v1/auth/signup",
                         json={"email": f"u{i}@example.com", "password": PASSWORD,
                               **device(f"s{i}")}).status_code
             for i in range(10)]
    assert 429 in codes


# -- telemetry privacy -------------------------------------------------------

def test_telemetry_refuses_private_content(client):
    a = make_user(client, "a@example.com", "a")
    client.post("/v1/telemetry/events", headers=auth(a["access_token"]), json={
        "events": [{"type": "TASK_COMPLETED",
                    "attrs": {"task_type": "web_search",
                              "transcript": "my private words",
                              "prompt": "secret prompt",
                              "content": "should never be stored"}}]})
    from nova_cloud.db import session_scope
    from nova_cloud.models import ActivityEvent
    from sqlalchemy import select
    with session_scope() as s:
        ev = s.scalar(select(ActivityEvent).where(
            ActivityEvent.type == "TASK_COMPLETED"))
        assert ev is not None
        stored = ev.attrs or {}
        assert stored.get("task_type") == "web_search"
        for banned in ("transcript", "prompt", "content"):
            assert banned not in stored


def test_telemetry_can_be_switched_off_by_the_user(client):
    a = make_user(client, "a@example.com", "a")
    client.post("/v1/sync/preferences", headers=auth(a["access_token"]),
                json={"preferences": {"telemetry_enabled": False}})
    r = client.post("/v1/telemetry/events", headers=auth(a["access_token"]),
                    json={"events": [{"type": "NOVA_STARTED"}]})
    assert r.get_json()["accepted"] == 0
    assert r.get_json()["reason"] == "telemetry_disabled"


def test_telemetry_requires_authentication(client):
    assert client.post("/v1/telemetry/events",
                       json={"events": []}).status_code == 401
