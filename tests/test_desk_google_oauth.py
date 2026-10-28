"""Google sign-in from the window's side: start, handoff, and the truth.

Google returns the browser to the cloud; the cloud redirects it to this
process's loopback route carrying a one-time state. These tests drive that
route on a bare Flask app with the real token guard and a fake cloud, so
nothing here touches a network or a browser. What they prove is what the
window is told: a session is adopted on success, and on failure the window
gets a reason instead of waiting five minutes for one that will never come.
"""
from __future__ import annotations

import pytest
import requests
from flask import Flask, jsonify, request

TOKEN = "test-desk-token"
AUTH = {"X-NOVA-Desk": TOKEN}
HOST = {"Host": "127.0.0.1:8765"}
LOOPBACK = "http://127.0.0.1:8765/api/account/google/callback"


class _Response:
    def __init__(self, status_code=200, payload=None, not_json=False):
        self.status_code = status_code
        self._payload = payload or {}
        self._not_json = not_json

    def json(self):
        if self._not_json:
            raise ValueError("not json")
        return self._payload


class FakeCloud:
    """Stands in for the cloud's start and callback endpoints."""

    def __init__(self):
        self.get_calls: list[dict] = []
        self.post_calls: list[dict] = []
        self.start = {"ok": True,
                      "auth_url": "https://accounts.google.com/o/oauth2/v2/auth?x",
                      "state": "state-1"}
        self.start_status = 200
        self.callback_status = 200
        self.callback = {"ok": True, "access_token": "at",
                         "user": {"id": "u1", "email": "sam@example.com",
                                  "display_name": "Sam", "email_verified": True}}
        self.offline = False

    def get(self, url, params=None, **kw):
        if self.offline:
            raise requests.RequestException("cloud down")
        self.get_calls.append({"url": url, "params": dict(params or {})})
        return _Response(self.start_status, self.start)

    def post(self, url, json=None, **kw):
        if self.offline:
            raise requests.RequestException("cloud down")
        self.post_calls.append({"url": url, "json": dict(json or {})})
        return _Response(self.callback_status, self.callback)


class FakeAccount:
    configured = True
    display_name = "Sam"

    def __init__(self):
        self.adopted = None
        self.events: list[tuple] = []
        self.signed_in = False

    def _adopt(self, payload):
        self.adopted = payload
        self.signed_in = True

    def emit(self, name, **kw):
        self.events.append((name, kw))

    def status(self):
        return {"configured": True, "signed_in": self.signed_in,
                "online": True, "user": None, "device_id": "d-1",
                "email_verified": True, "verification_required": False}


class _Desk:
    def __init__(self, client, cloud, acct, adopted_emails, bind_calls):
        self.client = client
        self.cloud = cloud
        self.acct = acct
        self.adopted_emails = adopted_emails
        self.bind_calls = bind_calls


@pytest.fixture()
def desk(monkeypatch):
    import nova_account
    from desk import account_api

    cloud = FakeCloud()
    acct = FakeAccount()
    adopted_emails: list[str] = []
    bind_calls: list = []

    monkeypatch.setenv("NOVA_CLOUD_URL", "http://cloud.test")
    monkeypatch.setattr(requests, "get", cloud.get)
    monkeypatch.setattr(requests, "post", cloud.post)
    monkeypatch.setattr(nova_account, "account", lambda: acct)
    monkeypatch.setattr(nova_account, "device_descriptor", lambda email="": {
        "device_id": "d-1", "device_secret": "s-1", "platform": "windows",
        "app_version": "0.1.0", "device_name": "machine"})
    monkeypatch.setattr(nova_account, "adopt_device_identity",
                        lambda email: adopted_emails.append(email))
    monkeypatch.setattr(account_api, "bind_signed_in_account",
                        lambda a: (bind_calls.append(a), False)[1])
    monkeypatch.setattr(account_api, "_adopt_identity", lambda a, m: None)
    monkeypatch.setattr(account_api, "pull_preferences_async", lambda: None)

    # A flow is module state; each test starts with none.
    account_api._google_flow = None
    account_api._google_outcome = None
    monkeypatch.setattr(account_api, "_GOOGLE_FLOW_TTL_S", 600)

    app = Flask(__name__)
    app.config["TESTING"] = True

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
        yield _Desk(c, cloud, acct, adopted_emails, bind_calls)
    account_api._google_flow = None
    account_api._google_outcome = None


# ── starting the flow ────────────────────────────────────────────────────────

def test_start_tells_the_cloud_where_to_send_the_browser(desk):
    r = desk.client.get("/api/account/google/start",
                        headers={**AUTH, **HOST})
    assert r.status_code == 200
    assert desk.cloud.get_calls[0]["params"]["client_redirect"] == LOOPBACK
    # The window now knows a flow is live.
    st = desk.client.get("/api/account/google/status", headers=AUTH).get_json()
    assert st["pending"] is True and st["error"] is None


def test_start_refuses_a_non_loopback_host(desk):
    """Only the local window may finish a sign-in."""
    r = desk.client.get("/api/account/google/start",
                        headers={**AUTH, "Host": "10.0.0.5:8765"})
    assert r.status_code == 503
    assert r.get_json()["error"] == "not_supported"
    assert desk.cloud.get_calls == []


def test_start_reports_an_unreachable_cloud(desk):
    desk.cloud.offline = True
    r = desk.client.get("/api/account/google/start", headers={**AUTH, **HOST})
    assert r.status_code == 503
    assert r.get_json()["error"] == "offline"


def test_start_translates_a_missing_google_client(desk):
    """The cloud's own message names environment variables. A person signing
    in gets plain words; the variable name goes to the log."""
    desk.cloud.start_status = 503
    desk.cloud.start = {"ok": False, "error": "oauth_not_configured",
                        "message": "Google OAuth client ID not configured "
                                   "(NOVA_GOOGLE_OAUTH_CLIENT_ID)"}
    r = desk.client.get("/api/account/google/start", headers={**AUTH, **HOST})
    assert r.status_code == 503
    body = r.get_json()
    assert body["error"] == "not_configured"
    assert "NOVA_GOOGLE_OAUTH_CLIENT_ID" not in body["message"]
    assert "email and password" in body["message"]


# ── the loopback handoff ─────────────────────────────────────────────────────

def _start(desk, state="state-1"):
    desk.cloud.start = {**desk.cloud.start, "state": state}
    desk.client.get("/api/account/google/start", headers={**AUTH, **HOST})
    return state


def test_handoff_adopts_the_session(desk):
    state = _start(desk)
    r = desk.client.get(f"/api/account/google/callback?code=abc&state={state}")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert "Signed in" in r.get_data(as_text=True)
    # The exchange went to the cloud with this machine's identity.
    sent = desk.cloud.post_calls[-1]["json"]
    assert sent["code"] == "abc" and sent["state"] == state
    assert sent["device_id"] == "d-1"
    assert desk.acct.adopted and desk.acct.signed_in
    assert desk.bind_calls, "the signed-in account was never bound"
    assert desk.adopted_emails == ["sam@example.com"], \
        "the device identity was left attached to nobody"
    assert desk.acct.events and desk.acct.events[-1][0] == "USER_LOGIN"


def test_handoff_needs_no_desk_token(desk):
    """A browser navigation sends no headers; the state is the credential."""
    state = _start(desk)
    r = desk.client.get(f"/api/account/google/callback?code=abc&state={state}")
    assert r.status_code == 200


def test_handoff_rejects_a_wrong_state_without_cancelling_the_flow(desk):
    state = _start(desk)
    r = desk.client.get("/api/account/google/callback?code=abc&state=nope")
    assert r.status_code == 400
    assert desk.cloud.post_calls == []
    # The real flow still works afterwards: a stray request must not be able
    # to cancel the sign-in the person is in the middle of.
    r = desk.client.get(f"/api/account/google/callback?code=abc&state={state}")
    assert r.status_code == 200


def test_handoff_state_is_single_use(desk):
    state = _start(desk)
    assert desk.client.get(
        f"/api/account/google/callback?code=abc&state={state}").status_code == 200
    r = desk.client.get(f"/api/account/google/callback?code=abc&state={state}")
    assert r.status_code == 400
    assert len(desk.cloud.post_calls) == 1


def test_handoff_refuses_an_expired_flow(desk, monkeypatch):
    state = _start(desk)
    from desk import account_api
    monkeypatch.setattr(account_api, "_GOOGLE_FLOW_TTL_S",
                        -1)            # everything is instantly stale
    r = desk.client.get(f"/api/account/google/callback?code=abc&state={state}")
    assert r.status_code == 400
    assert desk.cloud.post_calls == []


def test_handoff_refuses_when_no_flow_was_started(desk):
    r = desk.client.get("/api/account/google/callback?code=abc&state=state-1")
    assert r.status_code == 400
    assert desk.cloud.post_calls == []


# ── failure is reported to the window ───────────────────────────────────────

def test_denied_consent_is_reported(desk):
    state = _start(desk)
    r = desk.client.get(
        f"/api/account/google/callback?error=access_denied&state={state}")
    assert r.status_code == 400
    st = desk.client.get("/api/account/google/status", headers=AUTH).get_json()
    assert st["pending"] is False
    assert "cancelled" in st["error"]["message"].lower()


def test_cloud_failure_is_reported(desk):
    state = _start(desk)
    desk.cloud.callback_status = 502
    desk.cloud.callback = {"ok": False, "error": "token_exchange_failed",
                           "message": "Google is unavailable right now."}
    r = desk.client.get(f"/api/account/google/callback?code=abc&state={state}")
    assert r.status_code == 502
    st = desk.client.get("/api/account/google/status", headers=AUTH).get_json()
    assert st["error"]["message"] == "Google is unavailable right now."
    assert desk.acct.adopted is None, "a failed exchange signed someone in"


def test_offline_during_exchange_is_reported(desk):
    state = _start(desk)
    desk.cloud.offline = True
    r = desk.client.get(f"/api/account/google/callback?code=abc&state={state}")
    assert r.status_code == 503
    st = desk.client.get("/api/account/google/status", headers=AUTH).get_json()
    assert st["error"]["message"], "the window was left waiting with no reason"


def test_status_requires_the_desk_token(desk):
    assert desk.client.get("/api/account/google/status").status_code == 401


def test_pages_never_echo_the_code(desk):
    state = _start(desk)
    desk.cloud.callback_status = 400
    desk.cloud.callback = {"ok": False, "message": "No."}
    r = desk.client.get(
        f"/api/account/google/callback?code=<script>x</script>&state={state}")
    body = r.get_data(as_text=True)
    assert "<script" not in body.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
