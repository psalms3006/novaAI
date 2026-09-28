"""Instances, model access and updates on the NOVA backend.

The upstream (Google) is replaced by fakes at the two seams api_model exposes,
so these run offline and never spend a key. Signatures are real Ed25519.
"""
from __future__ import annotations

import base64
import json
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
    monkeypatch.setenv("NOVA_SECRET_KEY", "user-" + uuid.uuid4().hex)
    monkeypatch.setenv("NOVA_ADMIN_SECRET_KEY", "admin-" + uuid.uuid4().hex)
    monkeypatch.setenv("NOVA_GEMINI_API_KEY", "server-key-not-real")
    monkeypatch.setenv("NOVA_PLAN_FREE_GENERATE", "3")
    monkeypatch.setenv("NOVA_PLAN_FREE_LIVE", "2")
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


def device():
    return {"device_id": "dev-" + uuid.uuid4().hex[:12],
            "device_secret": "secret-" + uuid.uuid4().hex,
            "platform": "windows", "app_version": "1.0.0", "device_name": "PC"}


def signup(c, email):
    d = device()
    r = c.post("/v1/auth/signup", json={"email": email, "password": PASSWORD,
                                         "display_name": "Sam", **d})
    assert r.status_code == 201, r.get_json()
    j = r.get_json()
    return j["access_token"], d


def auth(t):
    return {"Authorization": f"Bearer {t}"}


# -- instance ------------------------------------------------------------------

def test_every_account_gets_its_own_instance_seeded_from_the_profile(client):
    t, _ = signup(client, "a@example.com")
    j = client.get("/v1/instance", headers=auth(t)).get_json()
    assert j["instance"]["id"]
    assert j["instance"]["profile"]["preferred_name"] == "Sam"
    assert j["instance"]["onboarding_completed"] is False


def test_profile_and_onboarding_state_persist_and_do_not_leak(client):
    ta, _ = signup(client, "a@example.com")
    tb, _ = signup(client, "b@example.com")
    r = client.patch("/v1/instance/profile", headers=auth(ta),
                     json={"preferred_name": "Psalms", "role": "developer",
                           "about": "I build NOVA."})
    assert r.status_code == 200
    client.post("/v1/instance/onboarding/complete", headers=auth(ta))

    a = client.get("/v1/instance", headers=auth(ta)).get_json()["instance"]
    b = client.get("/v1/instance", headers=auth(tb)).get_json()["instance"]
    assert a["onboarding_completed"] and a["profile"]["about"] == "I build NOVA."
    assert b["onboarding_completed"] is False and b["profile"]["about"] == ""
    assert a["id"] != b["id"]


def test_the_instance_is_chosen_by_the_token_not_the_request(client):
    ta, _ = signup(client, "a@example.com")
    tb, _ = signup(client, "b@example.com")
    b_id = client.get("/v1/instance", headers=auth(tb)).get_json()["instance"]["id"]
    # A client naming someone else's instance changes nothing about theirs.
    client.patch("/v1/instance/profile", headers=auth(ta),
                 json={"instance_id": b_id, "about": "hijacked"})
    b = client.get("/v1/instance", headers=auth(tb)).get_json()["instance"]
    assert b["profile"]["about"] == ""


def test_onboarding_completion_is_idempotent(client):
    t, _ = signup(client, "a@example.com")
    first = client.post("/v1/instance/onboarding/complete", headers=auth(t)).get_json()
    again = client.post("/v1/instance/onboarding/complete", headers=auth(t)).get_json()
    assert first["instance"]["onboarding_completed_at"] == again["instance"]["onboarding_completed_at"]


def test_invalid_role_is_refused(client):
    t, _ = signup(client, "a@example.com")
    r = client.patch("/v1/instance/profile", headers=auth(t), json={"role": "wizard"})
    assert r.status_code == 400


def test_offline_catalog_is_public_and_names_a_recommended_model(client):
    j = client.get("/v1/models/offline").get_json()
    cat = j["catalog"]
    assert cat["recommended"] in {m["id"] for m in cat["models"]}


# -- model gateway -------------------------------------------------------------

class _FakeUpstream:
    def __init__(self, payload, status=200):
        self.status_code = status
        self.headers = {"Content-Type": "application/json"}
        self._payload = payload
        self.content = json.dumps(payload).encode()

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=None):
        yield b'data: {"candidates": []}\n\n'

    def close(self):
        pass


@pytest.fixture()
def seen(monkeypatch):
    from nova_cloud import api_model
    calls = []

    def fake_forward(method, url, headers, body, stream):
        calls.append({"url": url, "headers": dict(headers), "body": body, "stream": stream})
        return _FakeUpstream({"candidates": [{"content": {"parts": [{"text": "hi"}]}}],
                              "usageMetadata": {"promptTokenCount": 7,
                                                "candidatesTokenCount": 2}})
    monkeypatch.setattr(api_model, "forward", fake_forward)
    return calls


def test_gateway_forwards_with_the_server_key_only(client, seen):
    t, _ = signup(client, "a@example.com")
    r = client.post("/gateway/v1beta/models/gemini-2.5-flash:generateContent?key=CLIENTKEY",
                    headers={"X-NOVA-Session": t, "x-goog-api-key": "sentinel"},
                    json={"contents": [{"parts": [{"text": "hello"}]}]})
    assert r.status_code == 200, r.get_data()
    assert r.get_json()["candidates"][0]["content"]["parts"][0]["text"] == "hi"
    call = seen[0]
    assert call["headers"]["x-goog-api-key"] == "server-key-not-real"
    assert "CLIENTKEY" not in call["url"] and "key=" not in call["url"]
    assert call["url"].startswith("https://generativelanguage.googleapis.com/v1beta/models/")


def test_gateway_requires_a_signed_in_account(client, seen):
    r = client.post("/gateway/v1beta/models/gemini-2.5-flash:generateContent", json={})
    assert r.status_code == 401
    assert seen == []


def test_gateway_refuses_models_and_methods_outside_the_allow_list(client, seen):
    t, _ = signup(client, "a@example.com")
    r1 = client.post("/gateway/v1beta/models/gemini-9-ultra:generateContent",
                     headers=auth(t), json={})
    r2 = client.post("/gateway/v1beta/tunedModels/x:generateContent", headers=auth(t), json={})
    r3 = client.post("/gateway/v1beta/models/gemini-2.5-flash:deleteEverything",
                     headers=auth(t), json={})
    assert (r1.status_code, r2.status_code, r3.status_code) == (403, 404, 404)
    assert seen == []


def test_daily_quota_is_enforced_per_instance_in_googles_error_shape(client, seen):
    ta, _ = signup(client, "a@example.com")
    tb, _ = signup(client, "b@example.com")
    url = "/gateway/v1beta/models/gemini-2.5-flash:generateContent"
    codes = [client.post(url, headers=auth(ta), json={}).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    over = client.post(url, headers=auth(ta), json={}).get_json()
    assert over["error"]["status"] == "RESOURCE_EXHAUSTED"
    # Another person's allowance is untouched.
    assert client.post(url, headers=auth(tb), json={}).status_code == 200

    inst = client.get("/v1/instance", headers=auth(ta)).get_json()["instance"]
    assert inst["usage_today"]["generate"] == 3


def test_gateway_records_token_counts_not_content(client, seen):
    t, _ = signup(client, "a@example.com")
    client.post("/gateway/v1beta/models/gemini-2.5-flash:generateContent",
                headers=auth(t), json={"contents": [{"parts": [{"text": "secret plan"}]}]})
    from nova_cloud.db import session_scope
    from nova_cloud.models import ModelUsage
    from sqlalchemy import select
    with session_scope() as s:
        row = s.scalars(select(ModelUsage)).one()
        assert (row.input_tokens, row.output_tokens) == (7, 2)


def test_streaming_is_relayed(client, seen):
    t, _ = signup(client, "a@example.com")
    r = client.post("/gateway/v1beta/models/gemini-2.5-flash:streamGenerateContent?alt=sse",
                    headers=auth(t), json={})
    assert r.status_code == 200
    assert b"candidates" in r.get_data()
    assert seen[0]["stream"] is True and "alt=sse" in seen[0]["url"]


def test_gateway_accepts_screenshot_sized_requests(client, seen):
    t, _ = signup(client, "a@example.com")
    big = {"contents": [{"parts": [{"inline_data": {"mime_type": "image/png",
                                                    "data": "A" * (3 * 1024 * 1024)}}]}]}
    r = client.post("/gateway/v1beta/models/gemini-2.5-flash:generateContent",
                    headers=auth(t), json=big)
    assert r.status_code == 200, r.status_code


# -- live token ----------------------------------------------------------------

def test_live_token_is_minted_locked_to_the_live_model(client, monkeypatch):
    from nova_cloud import api_model
    captured = {}

    class _Tokens:
        def create(self, config):
            captured.update(config)
            return type("T", (), {"name": "auth_tokens/abc123"})()

    monkeypatch.setattr(api_model, "genai_client_factory",
                        lambda key: type("C", (), {"auth_tokens": _Tokens(), "key": key})())
    t, _ = signup(client, "a@example.com")
    r = client.post("/v1/model/live-token", headers=auth(t))
    assert r.status_code == 200, r.get_json()
    j = r.get_json()
    assert j["token"] == "auth_tokens/abc123"
    assert "server-key" not in json.dumps(j)
    assert captured["live_connect_constraints"]["model"] == j["model"]
    # The fake must not hide a config the real SDK would reject: validate it
    # against the SDK's own schema (this caught a wrong field name once).
    from google.genai import types
    types.CreateAuthTokenConfig(**captured)
    # Two per day on this test plan, then refused.
    client.post("/v1/model/live-token", headers=auth(t))
    assert client.post("/v1/model/live-token", headers=auth(t)).status_code == 429


def test_live_token_says_so_when_the_server_has_no_key(client, monkeypatch):
    monkeypatch.setenv("NOVA_GEMINI_API_KEY", "")
    from nova_cloud import config as cfgmod
    cfgmod.reset_config()
    client.application.config["NOVA_CFG"] = cfgmod.config()
    t, _ = signup(client, "a@example.com")
    r = client.post("/v1/model/live-token", headers=auth(t))
    assert r.status_code == 503 and r.get_json()["error"] == "model_unavailable"


# -- updates -------------------------------------------------------------------

def _keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization as ser
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
    return priv, base64.b64encode(pub).decode()


def _manifest(version, min_supported="0.0.0"):
    return json.dumps({"version": version, "url": f"https://example.com/NOVA-{version}.exe",
                       "sha256": "ab" * 32, "size": 123, "min_supported": min_supported,
                       "notes": "test"}, sort_keys=True)


def _sign(priv, m):
    return base64.b64encode(priv.sign(m.encode())).decode()


def test_no_release_means_no_update(client):
    j = client.get("/v1/updates/check?version=1.0.0").get_json()
    assert j["update"] is None and j["latest"] is None


def test_a_newer_signed_release_is_offered_verbatim(client):
    from nova_cloud.api_updates import publish, verify_manifest
    priv, pub = _keypair()
    m = _manifest("1.1.0")
    publish(m, _sign(priv, m), pub)
    j = client.get("/v1/updates/check?version=1.0.0&device_id=abc").get_json()
    assert j["latest"] == "1.1.0" and j["update"]["manifest"] == m
    assert verify_manifest(j["update"]["manifest"], j["update"]["signature"], pub)
    # Up to date: nothing offered.
    assert client.get("/v1/updates/check?version=1.1.0").get_json()["update"] is None


def test_a_release_signed_with_another_key_cannot_be_published(client):
    from nova_cloud.api_updates import publish
    priv, _ = _keypair()
    _, other_pub = _keypair()
    m = _manifest("1.1.0")
    with pytest.raises(ValueError):
        publish(m, _sign(priv, m), other_pub)
    tampered = m.replace("NOVA-1.1.0", "EVIL")
    with pytest.raises(ValueError):
        publish(tampered, _sign(priv, m), _keypair()[1])


def test_rollout_holds_back_some_devices_but_never_an_unsupported_one(client):
    from nova_cloud.api_updates import publish
    priv, pub = _keypair()
    m = _manifest("2.0.0", min_supported="1.5.0")
    publish(m, _sign(priv, m), pub, rollout_percent=0)
    held = client.get("/v1/updates/check?version=1.6.0&device_id=x1").get_json()
    assert held["update"] is None and held["required"] is False
    forced = client.get("/v1/updates/check?version=1.0.0&device_id=x1").get_json()
    assert forced["required"] is True and forced["update"] is not None


def test_bad_version_is_rejected(client):
    assert client.get("/v1/updates/check?version=banana").status_code == 400


def test_an_update_result_is_recorded_for_the_device(client):
    t, d = signup(client, "a@example.com")
    r = client.post("/v1/updates/report", headers=auth(t),
                    json={"result": "rolled_back", "target": "1.1.0", "current": "1.0.0",
                          "error": "health check timed out"})
    assert r.status_code == 200
    from nova_cloud.db import session_scope
    from nova_cloud.models import DeviceUpdateState
    with session_scope() as s:
        st = s.get(DeviceUpdateState, d["device_id"])
        assert st.last_result == "rolled_back" and st.last_target == "1.1.0"
