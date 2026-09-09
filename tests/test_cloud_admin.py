"""Admin control plane: MFA, dashboard honesty, retention, feature flags.

The dashboard tests exist because a metric that is not derived from the
database is worse than no metric: it looks like knowledge and is not.
"""
from __future__ import annotations

import os
import tempfile
import time
import uuid

import pytest

PASSWORD = "a-long-enough-passphrase"


@pytest.fixture()
def env(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + os.path.join(tmp, "t.db"))
    monkeypatch.setenv("NOVA_ENV", "test")
    monkeypatch.setenv("NOVA_SECRET_KEY", "user-" + uuid.uuid4().hex)
    monkeypatch.setenv("NOVA_ADMIN_SECRET_KEY", "admin-" + uuid.uuid4().hex)
    monkeypatch.setenv("NOVA_ADMIN_REQUIRE_MFA", "1")

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


def auth(t):
    return {"Authorization": f"Bearer {t}"}


def seed_admin(email="root@nova.local", role="SUPER_ADMIN"):
    from nova_cloud import security as sec
    from nova_cloud.db import session_scope
    from nova_cloud.models import AdminUser
    with session_scope() as s:
        s.add(AdminUser(email=email, password_hash=sec.hash_password(PASSWORD),
                        role=role))


# -- MFA ---------------------------------------------------------------------

def test_mfa_is_required_before_an_admin_can_sign_in(env):
    seed_admin()
    r = env.post("/admin/api/auth/login",
                 json={"email": "root@nova.local", "password": PASSWORD})
    assert r.status_code == 403
    assert r.get_json()["error"] == "mfa_enrolment_required"


def test_mfa_enrolment_then_login(env):
    import pyotp
    seed_admin()

    start = env.post("/admin/api/auth/mfa/enrol",
                     json={"email": "root@nova.local", "password": PASSWORD})
    assert start.status_code == 200
    secret = start.get_json()["secret"]
    assert start.get_json()["stage"] == "confirm"

    # Not enrolled until the code is proven to work.
    assert env.post("/admin/api/auth/login",
                    json={"email": "root@nova.local",
                          "password": PASSWORD}).status_code == 403

    done = env.post("/admin/api/auth/mfa/enrol",
                    json={"email": "root@nova.local", "password": PASSWORD,
                          "totp": pyotp.TOTP(secret).now()})
    assert done.get_json()["stage"] == "enrolled"

    # Password alone is still not enough.
    r = env.post("/admin/api/auth/login",
                 json={"email": "root@nova.local", "password": PASSWORD})
    assert r.status_code == 401
    assert r.get_json()["error"] == "mfa_required"

    ok = env.post("/admin/api/auth/login",
                  json={"email": "root@nova.local", "password": PASSWORD,
                        "totp": pyotp.TOTP(secret).now()})
    assert ok.status_code == 200
    assert ok.get_json()["admin"]["role"] == "SUPER_ADMIN"


def test_a_wrong_totp_is_rejected(env):
    import pyotp
    seed_admin()
    secret = env.post("/admin/api/auth/mfa/enrol",
                      json={"email": "root@nova.local",
                            "password": PASSWORD}).get_json()["secret"]
    env.post("/admin/api/auth/mfa/enrol",
             json={"email": "root@nova.local", "password": PASSWORD,
                   "totp": pyotp.TOTP(secret).now()})
    r = env.post("/admin/api/auth/login",
                 json={"email": "root@nova.local", "password": PASSWORD,
                       "totp": "000000"})
    assert r.status_code == 401


def test_the_totp_seed_is_not_stored_in_the_clear(env):
    import pyotp
    seed_admin()
    secret = env.post("/admin/api/auth/mfa/enrol",
                      json={"email": "root@nova.local",
                            "password": PASSWORD}).get_json()["secret"]
    from nova_cloud.db import session_scope
    from nova_cloud.models import AdminUser
    from sqlalchemy import select
    with session_scope() as s:
        row = s.scalar(select(AdminUser))
        assert secret not in (row.totp_secret_enc or "")


def test_repeated_bad_passwords_lock_the_admin_account(env):
    seed_admin()
    for _ in range(6):
        env.post("/admin/api/auth/login",
                 json={"email": "root@nova.local", "password": "wrong-password"})
    r = env.post("/admin/api/auth/login",
                 json={"email": "root@nova.local", "password": PASSWORD})
    assert r.status_code == 423


# -- dashboard ---------------------------------------------------------------

def admin_token(env, role="SUPER_ADMIN", email="root@nova.local"):
    import pyotp
    seed_admin(email, role)
    secret = env.post("/admin/api/auth/mfa/enrol",
                      json={"email": email, "password": PASSWORD}).get_json()["secret"]
    env.post("/admin/api/auth/mfa/enrol",
             json={"email": email, "password": PASSWORD,
                   "totp": pyotp.TOTP(secret).now()})
    return env.post("/admin/api/auth/login",
                    json={"email": email, "password": PASSWORD,
                          "totp": pyotp.TOTP(secret).now()}).get_json()["access_token"]


def test_an_empty_platform_reports_zeros_not_invented_numbers(env):
    t = admin_token(env)
    m = env.get("/admin/api/dashboard", headers=auth(t)).get_json()["metrics"]
    assert m["users_total"] == 0
    assert m["devices_total"] == 0
    assert m["model_calls_24h"] == 0
    assert m["avg_latency_ms_24h"] is None      # unknown, not a made-up figure
    assert m["model_error_rate_24h"] == 0.0


def test_dashboard_counts_track_real_data(env):
    t = admin_token(env)
    before = env.get("/admin/api/dashboard", headers=auth(t)).get_json()["metrics"]

    r = env.post("/v1/auth/signup", json={
        "email": "u@example.com", "password": PASSWORD,
        "device_id": "d1", "device_secret": "s1", "platform": "windows"})
    tok = r.get_json()["access_token"]
    env.post("/v1/telemetry/events", headers=auth(tok), json={"events": [
        {"kind": "model_call", "provider": "gemini", "model": "flash",
         "latency_ms": 500, "status": "success"},
        {"kind": "model_call", "provider": "gemini", "model": "flash",
         "latency_ms": 700, "status": "error", "error_code": "MODEL_TIMEOUT"},
    ]})

    after = env.get("/admin/api/dashboard", headers=auth(t)).get_json()["metrics"]
    assert after["users_total"] == before["users_total"] + 1
    assert after["devices_total"] == before["devices_total"] + 1
    assert after["model_calls_24h"] == 2
    assert after["model_calls_failed_24h"] == 1
    assert after["model_error_rate_24h"] == 0.5
    assert after["avg_latency_ms_24h"] == 500      # successes only


def test_model_view_aggregates_per_provider(env):
    t = admin_token(env)
    r = env.post("/v1/auth/signup", json={
        "email": "u@example.com", "password": PASSWORD,
        "device_id": "d1", "device_secret": "s1"})
    tok = r.get_json()["access_token"]
    env.post("/v1/telemetry/events", headers=auth(tok), json={"events": [
        {"kind": "model_call", "provider": "ollama", "model": "qwen2.5:1.5b",
         "latency_ms": 1800, "status": "success", "offline": True},
        {"kind": "model_call", "provider": "ollama", "model": "qwen2.5:1.5b",
         "latency_ms": 2000, "status": "success", "offline": True},
    ]})
    j = env.get("/admin/api/models", headers=auth(t)).get_json()
    row = next(p for p in j["providers"] if p["provider"] == "ollama")
    assert row["requests"] == 2
    assert row["success_rate"] == 1.0
    assert row["avg_latency_ms"] == 1900
    assert row["offline_requests"] == 2


def test_health_labels_what_it_measured_versus_what_clients_reported(env):
    t = admin_token(env)
    j = env.get("/admin/api/health", headers=auth(t)).get_json()
    sources = {c["service"]: c["source"] for c in j["checks"]}
    assert sources["database"] == "measured"
    assert j["status"] in ("healthy", "degraded", "failing")


# -- feature flags -----------------------------------------------------------

def test_flag_rollout_is_stable_per_user(env):
    t = admin_token(env)
    env.put("/admin/api/flags/new_orb", headers=auth(t),
            json={"description": "new orb", "enabled": False,
                  "rollout_percent": 50})

    r = env.post("/v1/auth/signup", json={
        "email": "u@example.com", "password": PASSWORD,
        "device_id": "d1", "device_secret": "s1"})
    tok = r.get_json()["access_token"]

    first = env.get("/v1/sync/flags", headers=auth(tok)).get_json()["flags"]
    for _ in range(5):
        again = env.get("/v1/sync/flags", headers=auth(tok)).get_json()["flags"]
        assert again == first, "a user must not flip between requests"
    assert "new_orb" in first


def test_a_flag_turned_on_reaches_the_client(env):
    t = admin_token(env)
    env.put("/admin/api/flags/live_voice", headers=auth(t),
            json={"enabled": True})
    r = env.post("/v1/auth/signup", json={
        "email": "u@example.com", "password": PASSWORD,
        "device_id": "d1", "device_secret": "s1"})
    tok = r.get_json()["access_token"]
    flags = env.get("/v1/sync/flags", headers=auth(tok)).get_json()["flags"]
    assert flags["live_voice"] is True


# -- retention ---------------------------------------------------------------

def test_retention_deletes_only_what_is_past_its_window(env, monkeypatch):
    from nova_cloud.db import session_scope
    from nova_cloud.models import ActivityEvent
    from sqlalchemy import func, select

    now = time.time()
    with session_scope() as s:
        s.add(ActivityEvent(type="NOVA_STARTED", ts=now))                 # fresh
        s.add(ActivityEvent(type="NOVA_STARTED", ts=now - 200 * 86400))   # stale

    monkeypatch.setenv("NOVA_RET_TELEMETRY", "90")
    from nova_cloud import config as cfgmod, manage
    cfgmod.reset_config()

    class Args:
        dry_run = True
    manage.retention(Args())
    with session_scope() as s:
        assert int(s.scalar(select(func.count()).select_from(ActivityEvent))) == 2

    Args.dry_run = False
    manage.retention(Args())
    with session_scope() as s:
        remaining = s.scalars(select(ActivityEvent)).all()
    assert len(remaining) == 1
    assert remaining[0].ts > now - 86400


def test_account_deletion_removes_personal_data_but_keeps_counts_honest(env):
    r = env.post("/v1/auth/signup", json={
        "email": "u@example.com", "password": PASSWORD,
        "device_id": "d1", "device_secret": "s1"})
    j = r.get_json()
    tok = j["access_token"]
    env.post("/v1/sync/preferences", headers=auth(tok),
             json={"preferences": {"theme": "dark"}})
    env.post("/v1/telemetry/events", headers=auth(tok), json={"events": [
        {"kind": "model_call", "provider": "gemini", "model": "flash",
         "latency_ms": 100}]})

    assert env.post("/v1/account/delete", headers=auth(tok),
                    json={"password": PASSWORD}).status_code == 400   # needs confirm
    ok = env.post("/v1/account/delete", headers=auth(tok),
                  json={"password": PASSWORD, "confirm": "DELETE"})
    assert ok.status_code == 200

    from nova_cloud.db import session_scope
    from nova_cloud.models import Device, ModelCall, Preference, User
    from sqlalchemy import func, select
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(User)) == 0
        assert s.scalar(select(func.count()).select_from(Device)) == 0
        assert s.scalar(select(func.count()).select_from(Preference)) == 0
        # The call still counts towards platform volume, but is no longer
        # attached to anybody.
        call = s.scalar(select(ModelCall))
        assert call is not None
        assert call.user_id is None and call.device_id is None


def test_deleting_an_account_needs_the_password(env):
    r = env.post("/v1/auth/signup", json={
        "email": "u@example.com", "password": PASSWORD,
        "device_id": "d1", "device_secret": "s1"})
    tok = r.get_json()["access_token"]
    bad = env.post("/v1/account/delete", headers=auth(tok),
                   json={"password": "not-it", "confirm": "DELETE"})
    assert bad.status_code == 401


# -- configuration safety ----------------------------------------------------

def test_production_refuses_to_start_without_signing_keys(monkeypatch):
    monkeypatch.setenv("NOVA_ENV", "production")
    monkeypatch.delenv("NOVA_SECRET_KEY", raising=False)
    monkeypatch.delenv("NOVA_ADMIN_SECRET_KEY", raising=False)
    from nova_cloud import config as cfgmod
    cfgmod.reset_config()
    with pytest.raises(RuntimeError, match="NOVA_SECRET_KEY"):
        cfgmod.config()
    cfgmod.reset_config()


def test_the_two_signing_keys_must_differ(monkeypatch):
    monkeypatch.setenv("NOVA_ENV", "test")
    monkeypatch.setenv("NOVA_SECRET_KEY", "same-key-for-both")
    monkeypatch.setenv("NOVA_ADMIN_SECRET_KEY", "same-key-for-both")
    from nova_cloud import config as cfgmod
    cfgmod.reset_config()
    with pytest.raises(RuntimeError, match="must differ"):
        cfgmod.config()
    cfgmod.reset_config()
