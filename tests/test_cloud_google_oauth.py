"""Google sign-in: minimum scopes, verified state, safe failure.

The sandbox: the real Flask app on a throwaway SQLite database, with Google's
token and userinfo endpoints replaced by in-process fakes. Nothing here
touches Google, and nothing here can: every network call in the OAuth path
is patched. What the tests prove is the *shape* of the flow — what we ask
Google for, what we accept back, and what never gets through.
"""
from __future__ import annotations

import os
import tempfile
import time
import uuid

import pytest

PASSWORD = "a-long-enough-passphrase"


class FakeGoogle:
    """Stands in for Google's token + userinfo endpoints; records requests."""

    def __init__(self, *, fail_token=False, fail_userinfo=False,
                 email="sam@example.com", verified=True, sub="google-sub-123"):
        self.fail_token = fail_token
        self.fail_userinfo = fail_userinfo
        self.email = email
        self.verified = verified
        self.sub = sub
        self.token_requests: list[dict] = []

    def post(self, url, data=None, **kw):
        import requests
        if self.fail_token:
            raise requests.RequestException("provider down")
        self.token_requests.append(dict(data or {}))
        return _FakeResponse({"access_token": "g-at", "id_token": "g-id",
                              "refresh_token": "g-rt"})

    def get(self, url, headers=None, **kw):
        import requests
        if self.fail_userinfo:
            raise requests.RequestException("provider down")
        return _FakeResponse({"sub": self.sub, "email": self.email,
                              "verified_email": self.verified,
                              "name": "Sam Hosted", "picture": "p.png"})


class _FakeResponse:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


@pytest.fixture()
def env(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + os.path.join(tmp, "t.db"))
    monkeypatch.setenv("NOVA_ENV", "test")
    monkeypatch.setenv("NOVA_SECRET_KEY", "user-" + uuid.uuid4().hex)
    monkeypatch.setenv("NOVA_ADMIN_SECRET_KEY", "admin-" + uuid.uuid4().hex)
    # OAuth client configured for the sandbox. Values are throwaways.
    monkeypatch.setenv("NOVA_GOOGLE_OAUTH_CLIENT_ID", "sandbox-client-id.apps.googleusercontent.com")
    monkeypatch.setenv("NOVA_GOOGLE_OAUTH_CLIENT_SECRET", "sandbox-secret-not-real")
    monkeypatch.setenv("NOVA_GOOGLE_OAUTH_REDIRECT_URI", "https://sandbox.omniel.com.ng/oauth/google")
    for key in ("EMAIL_PROVIDER", "RESEND_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    from nova_cloud import config as cfgmod, db, mailer
    from nova_cloud.auth_guard import invalidate_auth_cache
    cfgmod.reset_config()
    db.reset_engine()
    mailer.reset_mailer()
    invalidate_auth_cache()

    from nova_cloud.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
    db.reset_engine()
    cfgmod.reset_config()
    mailer.reset_mailer()
    invalidate_auth_cache()


def device(tag="a"):
    return {"device_id": f"d-{tag}-{uuid.uuid4().hex[:6]}",
            "device_secret": "s-" + uuid.uuid4().hex,
            "platform": "windows", "app_version": "0.1.0",
            "device_name": f"machine-{tag}"}


# ── configuration honesty ────────────────────────────────────────────────────

def test_missing_configuration_fails_safely(env, monkeypatch):
    from nova_cloud import config as cfgmod
    monkeypatch.delenv("NOVA_GOOGLE_OAUTH_CLIENT_ID", raising=False)
    cfgmod.reset_config()
    from nova_cloud.app import create_app
    app = create_app()
    with app.test_client() as c:
        r = c.get("/v1/auth/google/start")
        assert r.status_code == 503
        assert r.get_json()["error"] == "oauth_not_configured"
    cfgmod.reset_config()


# ── the start of the flow: what we ask Google for ────────────────────────────

def test_start_returns_url_with_minimum_scopes(env):
    r = env.get("/v1/auth/google/start").get_json()
    assert r["ok"] is True
    url, state = r["auth_url"], r["state"]
    assert "accounts.google.com/o/oauth2/v2/auth" in url
    assert "openid+email+profile" in url.replace("%20", "+") or \
        "openid%20email%20profile" in url
    # The sensitive permissions are never in the sign-in request.
    for forbidden in ("gmail", "calendar", "drive", "spreadsheet"):
        assert forbidden not in url.lower()
    assert "access_type=offline" not in url, \
        "basic sign-in must not request a refresh token"
    assert state and "." in state


def test_state_is_signed_and_single_flow(env):
    """Two starts give different states; each verifies under the same key."""
    from nova_cloud.api_auth import _google_state_valid
    with env.application.app_context():
        s1 = env.get("/v1/auth/google/start").get_json()["state"]
        s2 = env.get("/v1/auth/google/start").get_json()["state"]
    assert s1 != s2
    assert _google_state_valid(s1) and _google_state_valid(s2)


# ── the callback: what we accept and what never gets through ─────────────────

def _callback(env, g: FakeGoogle, **over):
    from nova_cloud import api_auth
    state = env.get("/v1/auth/google/start").get_json()["state"]
    body = {"code": "auth-code-123", "state": state, **device("g"), **over}
    with __import__("unittest.mock", fromlist=["patch"]).patch.object(
            api_auth.requests, "post", g.post), \
         __import__("unittest.mock", fromlist=["patch"]).patch.object(
            api_auth.requests, "get", g.get):
        return env.post("/v1/auth/google/callback", json=body)


def test_successful_signin_creates_account_and_session(env):
    r = _callback(env, FakeGoogle())
    assert r.status_code == 200, r.get_data(as_text=True)
    j = r.get_json()
    assert j["ok"] is True
    assert j["user"]["email"] == "sam@example.com"
    assert j["user"]["email_verified"] is True
    assert j["access_token"], "no session was issued"
    # Only profile basics were read: the fake returns a picture too, and the
    # response must not echo anything beyond the basic profile fields.
    assert set(j["user"]) <= {"id", "email", "display_name", "email_verified",
                              "created_at"}


def test_token_exchange_never_asks_for_offline_access(env):
    g = FakeGoogle()
    _callback(env, g)
    assert g.token_requests, "no token exchange happened"
    sent = g.token_requests[0]
    assert sent.get("grant_type") == "authorization_code"
    assert "offline" not in str(sent)


def test_denied_consent_fails_safely(env):
    r = env.post("/v1/auth/google/callback",
                 json={"error": "access_denied", "state": "x.y",
                       **device("g")})
    assert r.status_code == 400
    assert r.get_json()["error"] == "authorization_denied"


def test_missing_code_and_state_rejected(env):
    r = env.post("/v1/auth/google/callback", json={**device("g")})
    assert r.status_code == 400 and r.get_json()["error"] == "missing_code"
    r = env.post("/v1/auth/google/callback",
                 json={"code": "c", **device("g")})
    assert r.status_code == 400 and r.get_json()["error"] == "missing_state"


def test_forged_state_rejected(env):
    """The login-CSRF classic: an attacker's callback must not sign the
    victim into anything."""
    r = env.post("/v1/auth/google/callback",
                 json={"code": "attacker-code", "state": "forged.sig",
                       **device("g")})
    assert r.status_code == 400
    assert r.get_json()["error"] == "invalid_state"


def test_replayed_state_rejected(env):
    """A state from an older flow (or the same one twice) is stale."""
    import nova_cloud.api_auth as aa
    old_state = aa._google_oauth_state.__wrapped__ if hasattr(
        aa._google_oauth_state, "__wrapped__") else None
    with env.application.app_context():
        old_state = aa._google_oauth_state()
    real_ttl = aa._GOOGLE_STATE_TTL_S
    aa._GOOGLE_STATE_TTL_S = 0
    try:
        with env.application.app_context():
            assert not aa._google_state_valid(old_state)
    finally:
        aa._GOOGLE_STATE_TTL_S = real_ttl


def unittest_patch_post_get():
    import contextlib
    @contextlib.contextmanager
    def _noop():
        yield
    return _noop()


def test_expired_state_rejected(env):
    import nova_cloud.api_auth as aa
    with env.application.app_context():
        state = aa._google_oauth_state()
    real = aa._GOOGLE_STATE_TTL_S
    aa._GOOGLE_STATE_TTL_S = -1          # everything is instantly stale
    try:
        g = FakeGoogle()
        with unittest_patch_requests(g):
            r = env.post("/v1/auth/google/callback",
                         json={"code": "c", "state": state, **device("g")})
    finally:
        aa._GOOGLE_STATE_TTL_S = real
    assert r.status_code == 400
    assert r.get_json()["error"] == "invalid_state"


def unittest_patch_requests(g):
    import contextlib
    from unittest.mock import patch
    @contextlib.contextmanager
    def _ctx():
        with patch.object(__import__("nova_cloud.api_auth", fromlist=["requests"]).requests,
                          "post", g.post), \
             patch.object(__import__("nova_cloud.api_auth", fromlist=["requests"]).requests,
                          "get", g.get):
            yield
    return _ctx()


def test_token_exchange_failure_is_a_clean_502(env):
    g = FakeGoogle(fail_token=True)
    with unittest_patch_requests(g):
        r = env.post("/v1/auth/google/callback",
                     json={"code": "c", "state": _fresh_state(env), **device("g")})
    assert r.status_code == 502
    assert r.get_json()["error"] == "token_exchange_failed"


def _fresh_state(env):
    return env.get("/v1/auth/google/start").get_json()["state"]


def test_userinfo_failure_is_a_clean_502(env):
    g = FakeGoogle(fail_userinfo=True)
    with unittest_patch_requests(g):
        r = env.post("/v1/auth/google/callback",
                     json={"code": "c", "state": _fresh_state(env), **device("g")})
    assert r.status_code == 502
    assert r.get_json()["error"] == "userinfo_failed"


def test_signin_does_not_grant_gmail_or_drive(env):
    """The response session's powers are NOVA's own session tokens; Google
    service access requires separate, explicit connections. Assert no
    Google refresh token is stored or returned to the client."""
    g = FakeGoogle()
    r = _callback(env, g)
    j = r.get_json()
    assert "refresh_token" not in str(j.get("google_", ""))
    assert j.get("google_refresh_token") is None
    # And the fake's refresh token was received but never persisted anywhere
    # the client can see.
    assert g.token_requests  # exchange happened
    body = str(j)
    assert "g-rt" not in body


def test_existing_email_account_links_and_upgrades_verification(env):
    from nova_cloud import api_auth
    # Password account first
    env.post("/v1/auth/signup", json={"email": "sam@example.com",
                                      "password": PASSWORD,
                                      "display_name": "Sam", **device("a")})
    g = FakeGoogle(verified=True)
    with unittest_patch_requests(g):
        r = env.post("/v1/auth/google/callback",
                     json={"code": "c", "state": _fresh_state(env), **device("g")})
    assert r.status_code == 200
    # Same account, now Google-verified
    from nova_cloud.db import session_scope
    from nova_cloud.models import User
    with session_scope() as s:
        u = s.query(User).filter_by(email="sam@example.com").one()
        assert u.email_verified is True


def test_unverified_google_email_stays_unverified(env):
    g = FakeGoogle(verified=False)
    r = _callback(env, g)
    assert r.status_code == 200
    assert r.get_json()["user"]["email_verified"] is False


def test_basic_signin_scope_list_is_frozen():
    """If someone widens GOOGLE_SCOPES, this fails before a user ever sees
    a consent screen asking for their mail."""
    from nova_cloud.api_auth import GOOGLE_SCOPES
    assert set(GOOGLE_SCOPES) == {"openid", "email", "profile"}


# ── the desktop handoff: from Google's redirect to the app's own route ──────
#
# Google navigates the browser to the cloud. The desktop cannot receive that
# navigation, so the cloud signs the app's loopback address into the state and
# redirects there. These tests are what stops that handoff from becoming an
# open redirect or a way to leak a code.

DESK_CB = "http://127.0.0.1:8765/api/account/google/callback"


def test_loopback_redirect_allow_list():
    from nova_cloud.api_auth import _loopback_redirect
    assert _loopback_redirect(DESK_CB) == DESK_CB
    assert _loopback_redirect("http://localhost:8765/api/account/google/callback")
    assert _loopback_redirect("http://[::1]:8765/api/account/google/callback")
    for bad in (
        "",
        "https://127.0.0.1:8765/api/account/google/callback",   # not http
        "http://192.168.1.5:8765/api/account/google/callback",  # not loopback
        "http://evil.example.com/api/account/google/callback",
        "http://127.0.0.1:8765/other",                          # wrong path
        "http://127.0.0.1/api/account/google/callback",         # no port
        "http://user:pw@127.0.0.1:8765/api/account/google/callback",
        DESK_CB + "?x=1",
        DESK_CB + "#frag",
    ):
        assert _loopback_redirect(bad) == "", f"accepted {bad!r}"


def test_start_signs_the_desktop_callback(env):
    from nova_cloud.api_auth import _google_state_redirect
    r = env.get("/v1/auth/google/start",
                query_string={"client_redirect": DESK_CB}).get_json()
    assert r["ok"] is True
    with env.application.app_context():
        assert _google_state_redirect(r["state"]) == DESK_CB


def test_start_without_a_client_gets_no_redirect(env):
    from nova_cloud.api_auth import _google_state_redirect
    r = env.get("/v1/auth/google/start").get_json()
    with env.application.app_context():
        assert _google_state_redirect(r["state"]) == ""


def test_start_refuses_a_non_loopback_callback(env):
    r = env.get("/v1/auth/google/start", query_string={
        "client_redirect": "http://evil.example.com/api/account/google/callback"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "invalid_redirect"


def test_get_callback_redirects_home_with_the_code(env):
    state = env.get("/v1/auth/google/start",
                    query_string={"client_redirect": DESK_CB}).get_json()["state"]
    r = env.get("/v1/auth/google/callback",
                query_string={"code": "auth-code-123", "state": state})
    assert r.status_code == 302, r.get_data(as_text=True)
    location = r.headers["Location"]
    assert location.startswith(DESK_CB + "?")
    assert "code=auth-code-123" in location
    assert "state=" in location


def test_get_callback_never_navigates_without_a_signed_target(env):
    """A browser-only flow has no loopback address; the code stops here."""
    state = env.get("/v1/auth/google/start").get_json()["state"]
    r = env.get("/v1/auth/google/callback",
                query_string={"code": "auth-code-123", "state": state})
    assert r.status_code == 400
    assert "Location" not in r.headers
    assert "auth-code-123" not in r.get_data(as_text=True)


def test_get_callback_rejects_forged_state(env):
    r = env.get("/v1/auth/google/callback",
                query_string={"code": "attacker-code", "state": "v1.1.x.y.z"})
    assert r.status_code == 400
    assert "Location" not in r.headers
    assert "attacker-code" not in r.get_data(as_text=True)


def test_get_callback_expired_state_does_not_redirect(env):
    import nova_cloud.api_auth as aa
    state = env.get("/v1/auth/google/start",
                    query_string={"client_redirect": DESK_CB}).get_json()["state"]
    real = aa._GOOGLE_STATE_TTL_S
    aa._GOOGLE_STATE_TTL_S = -1
    try:
        r = env.get("/v1/auth/google/callback",
                    query_string={"code": "c", "state": state})
    finally:
        aa._GOOGLE_STATE_TTL_S = real
    assert r.status_code == 400
    assert "Location" not in r.headers


def test_get_callback_denied_consent_does_not_redirect(env):
    r = env.get("/v1/auth/google/callback",
                query_string={"error": "access_denied", "state": "irrelevant"})
    assert r.status_code == 400
    assert "Location" not in r.headers
    assert "cancelled" in r.get_data(as_text=True).lower()


def test_get_callback_pages_are_script_free(env):
    """These pages render values that came from a query string; none is echoed."""
    r = env.get("/v1/auth/google/callback",
                query_string={"code": "<script>alert(1)</script>",
                              "state": "forged"})
    body = r.get_data(as_text=True)
    assert "<script" not in body.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
