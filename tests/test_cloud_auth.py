"""Account lifecycle: signup, login, refresh rotation, logout, reset.

These run against the real Flask app and a real (temporary) database. Nothing
is mocked, because the things worth testing here -- rotation, revocation,
epochs -- live in the database.
"""
from __future__ import annotations

import os
import tempfile
import uuid

import pytest


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + os.path.join(tmp, "t.db"))
    monkeypatch.setenv("NOVA_ENV", "test")
    monkeypatch.setenv("NOVA_SECRET_KEY", "test-user-key-" + uuid.uuid4().hex)
    monkeypatch.setenv("NOVA_ADMIN_SECRET_KEY", "test-admin-key-" + uuid.uuid4().hex)
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


PASSWORD = "a-long-enough-passphrase"


def device(n: int = 1) -> dict:
    return {"device_id": f"dev-{n:04d}-{uuid.uuid4().hex[:8]}",
            "device_secret": "secret-" + uuid.uuid4().hex,
            "platform": "windows", "app_version": "0.1.0",
            "device_name": f"Test machine {n}"}


def signup(client, email="sam@example.com", **extra):
    body = {"email": email, "password": PASSWORD,
            "display_name": "Sam", **device(1), **extra}
    return client.post("/v1/auth/signup", json=body)


# -- signup ------------------------------------------------------------------

def test_signup_returns_tokens_and_device(client):
    r = signup(client)
    assert r.status_code == 201, r.get_json()
    j = r.get_json()
    assert j["ok"] and j["access_token"] and j["refresh_token"]
    assert j["user"]["email"] == "sam@example.com"
    assert j["device"]["id"]
    # The user id must be opaque, not the email.
    assert "@" not in j["user"]["id"]


def test_password_is_never_stored_in_plaintext(client):
    signup(client)
    from nova_cloud.db import session_scope
    from nova_cloud.models import User
    from sqlalchemy import select
    with session_scope() as s:
        u = s.scalar(select(User))
        assert PASSWORD not in u.password_hash
        assert u.password_hash.startswith("$argon2id$")


def test_weak_password_is_refused(client):
    r = client.post("/v1/auth/signup", json={
        "email": "x@example.com", "password": "short", **device(1)})
    assert r.status_code == 400
    assert r.get_json()["error"] == "weak_password"


def test_duplicate_email_is_refused(client):
    signup(client)
    r = signup(client, email="SAM@example.com")   # case-insensitive
    assert r.status_code == 409


def test_signup_requires_a_device(client):
    r = client.post("/v1/auth/signup",
                    json={"email": "a@example.com", "password": PASSWORD})
    assert r.status_code == 400


# -- login -------------------------------------------------------------------

def test_login_succeeds_and_reuses_the_device(client):
    d = device(1)
    client.post("/v1/auth/signup", json={"email": "sam@example.com",
                                         "password": PASSWORD, **d})
    r = client.post("/v1/auth/login", json={"email": "sam@example.com",
                                            "password": PASSWORD, **d})
    assert r.status_code == 200
    assert r.get_json()["device"]["id"] == d["device_id"]


def test_wrong_password_is_rejected(client):
    signup(client)
    r = client.post("/v1/auth/login", json={"email": "sam@example.com",
                                            "password": "not-the-password",
                                            **device(1)})
    assert r.status_code == 401


def test_unknown_and_wrong_password_are_indistinguishable(client):
    signup(client)
    a = client.post("/v1/auth/login", json={"email": "sam@example.com",
                                            "password": "wrong-one-here",
                                            **device(1)}).get_json()
    b = client.post("/v1/auth/login", json={"email": "nobody@example.com",
                                            "password": "wrong-one-here",
                                            **device(2)}).get_json()
    assert a["message"] == b["message"] and a["error"] == b["error"]


def test_a_device_secret_cannot_be_guessed(client):
    """Knowing a device_id is not enough to claim that installation."""
    d = device(1)
    client.post("/v1/auth/signup", json={"email": "sam@example.com",
                                         "password": PASSWORD, **d})
    impostor = dict(d, device_secret="a-different-secret")
    r = client.post("/v1/auth/login", json={"email": "sam@example.com",
                                            "password": PASSWORD, **impostor})
    assert r.status_code == 403


def test_a_device_cannot_be_stolen_by_another_account(client):
    d = device(1)
    client.post("/v1/auth/signup", json={"email": "sam@example.com",
                                         "password": PASSWORD, **d})
    client.post("/v1/auth/signup", json={"email": "other@example.com",
                                         "password": PASSWORD, **device(2)})
    r = client.post("/v1/auth/login", json={"email": "other@example.com",
                                            "password": PASSWORD, **d})
    assert r.status_code == 403


# -- refresh -----------------------------------------------------------------

def test_refresh_rotates_the_token(client):
    j = signup(client).get_json()
    first = j["refresh_token"]
    r = client.post("/v1/auth/refresh", json={"refresh_token": first})
    assert r.status_code == 200
    second = r.get_json()["refresh_token"]
    assert second != first, "refresh token must rotate"


def test_reusing_a_rotated_refresh_token_kills_the_family(client):
    """Leak detection: the old token showing up again means it was stolen."""
    j = signup(client).get_json()
    old = j["refresh_token"]
    new = client.post("/v1/auth/refresh",
                      json={"refresh_token": old}).get_json()["refresh_token"]

    replay = client.post("/v1/auth/refresh", json={"refresh_token": old})
    assert replay.status_code == 401

    # ...and the legitimate token is dead too, because we cannot tell which
    # party is the thief.
    after = client.post("/v1/auth/refresh", json={"refresh_token": new})
    assert after.status_code == 401


def test_refresh_with_garbage_is_rejected(client):
    r = client.post("/v1/auth/refresh", json={"refresh_token": "nope"})
    assert r.status_code == 401


# -- me / logout -------------------------------------------------------------

def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_me_requires_a_token(client):
    assert client.get("/v1/auth/me").status_code == 401


def test_me_returns_the_account(client):
    j = signup(client).get_json()
    r = client.get("/v1/auth/me", headers=auth(j["access_token"]))
    assert r.status_code == 200
    assert r.get_json()["user"]["email"] == "sam@example.com"


def test_logout_revokes_only_that_session(client):
    j = signup(client).get_json()
    client.post("/v1/auth/logout", json={"refresh_token": j["refresh_token"]})
    r = client.post("/v1/auth/refresh", json={"refresh_token": j["refresh_token"]})
    assert r.status_code == 401


def test_logout_all_invalidates_existing_access_tokens(client):
    j = signup(client).get_json()
    tok = j["access_token"]
    assert client.get("/v1/auth/me", headers=auth(tok)).status_code == 200
    client.post("/v1/auth/logout_all", headers=auth(tok))
    from nova_cloud.auth_guard import invalidate_auth_cache
    invalidate_auth_cache()
    # The epoch moved, so the token that was valid a moment ago is not.
    assert client.get("/v1/auth/me", headers=auth(tok)).status_code == 401


# -- password reset ----------------------------------------------------------

def test_forgot_password_does_not_reveal_whether_an_account_exists(client):
    signup(client)
    a = client.post("/v1/auth/password/forgot", json={"email": "sam@example.com"})
    b = client.post("/v1/auth/password/forgot", json={"email": "ghost@example.com"})
    assert a.get_json()["message"] == b.get_json()["message"]
    assert a.status_code == b.status_code


def test_password_reset_works_and_ends_all_sessions(client):
    j = signup(client).get_json()
    tok = j["access_token"]
    reset = client.post("/v1/auth/password/forgot",
                        json={"email": "sam@example.com"}).get_json()
    token = reset["dev_reset_token"]

    r = client.post("/v1/auth/password/reset",
                    json={"token": token, "password": "a-brand-new-passphrase"})
    assert r.status_code == 200

    from nova_cloud.auth_guard import invalidate_auth_cache
    invalidate_auth_cache()
    assert client.get("/v1/auth/me", headers=auth(tok)).status_code == 401

    ok = client.post("/v1/auth/login", json={"email": "sam@example.com",
                                             "password": "a-brand-new-passphrase",
                                             **device(1)})
    assert ok.status_code == 200


def test_a_reset_token_is_single_use(client):
    signup(client)
    token = client.post("/v1/auth/password/forgot",
                        json={"email": "sam@example.com"}
                        ).get_json()["dev_reset_token"]
    client.post("/v1/auth/password/reset",
                json={"token": token, "password": "first-new-passphrase"})
    again = client.post("/v1/auth/password/reset",
                        json={"token": token, "password": "second-new-passphrase"})
    assert again.status_code == 400
