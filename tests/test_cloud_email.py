"""Account email, and the honesty rules around it.

The failure this suite is built to prevent: NOVA telling a user to check their
inbox when nothing was ever sent. Verification and reset either work or say
plainly that they are not configured -- there is no third state where the UI
implies success.
"""
from __future__ import annotations

import os
import tempfile
import uuid

import pytest

PASSWORD = "a-long-enough-passphrase"


class FakeProvider:
    """Stands in for Resend/SMTP and records what was actually sent."""

    def __init__(self, fail: bool = False):
        self.messages: list[dict] = []
        self.fail = fail

    def __call__(self, cfg, to, subject, text, html_body):
        if self.fail:
            raise RuntimeError("provider is down")
        self.messages.append({"to": to, "subject": subject, "text": text,
                              "html": html_body, "from": cfg.from_address})


@pytest.fixture()
def env(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + os.path.join(tmp, "t.db"))
    monkeypatch.setenv("NOVA_ENV", "test")
    monkeypatch.setenv("NOVA_SECRET_KEY", "user-" + uuid.uuid4().hex)
    monkeypatch.setenv("NOVA_ADMIN_SECRET_KEY", "admin-" + uuid.uuid4().hex)
    for key in ("EMAIL_PROVIDER", "RESEND_API_KEY", "SMTP_HOST", "EMAIL_FROM",
                "SUPPORT_EMAIL", "ADMIN_EMAIL", "APP_BASE_URL"):
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


def configure_email(monkeypatch, provider="resend"):
    """Point the mailer at a fake provider and return the recorder."""
    from nova_cloud import mailer
    monkeypatch.setenv("EMAIL_PROVIDER", provider)
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("EMAIL_FROM", "NOVA <hello@omniel.com.ng>")
    monkeypatch.setenv("SUPPORT_EMAIL", "support@omniel.com.ng")
    monkeypatch.setenv("APP_BASE_URL", "https://nova.omniel.com.ng")
    mailer.reset_mailer()
    fake = FakeProvider()
    monkeypatch.setitem(mailer._PROVIDERS, provider, fake)
    return fake


def device(tag="a"):
    return {"device_id": f"d-{tag}-{uuid.uuid4().hex[:6]}",
            "device_secret": "s-" + uuid.uuid4().hex,
            "platform": "windows", "app_version": "0.1.0",
            "device_name": f"machine-{tag}"}


def signup(env, email="sam@example.com", **kw):
    return env.post("/v1/auth/signup", json={
        "email": email, "password": PASSWORD, "display_name": "Sam",
        **device("a"), **kw})


def auth(t):
    return {"Authorization": f"Bearer {t}"}


def _unverify(user_id: str) -> None:
    """Put the account back in the state a real unverified signup produces."""
    from nova_cloud.db import session_scope
    from nova_cloud.models import User
    with session_scope() as s:
        s.get(User, user_id).email_verified = False


# -- configuration honesty ---------------------------------------------------

def test_with_no_provider_nothing_claims_to_have_been_sent(env):
    r = signup(env)
    j = r.get_json()
    assert j["email_delivery"]["email_configured"] is False
    assert j["email_delivery"]["sent"] is False
    assert j["email_delivery"]["email_provider"] == "none"
    # Nothing was sent and nothing claims otherwise.
    assert "check your inbox" not in str(j).lower()


def test_forgot_password_does_not_claim_delivery_when_unconfigured(env):
    signup(env)
    j = env.post("/v1/auth/password/forgot",
                 json={"email": "sam@example.com"}).get_json()
    assert j["email_configured"] is False
    # Unconfigured and non-production: the token is handed back so the flow
    # can still be completed by a developer.
    assert j["dev_reset_token"]


def test_the_reset_response_is_identical_for_unknown_addresses(env, monkeypatch):
    """Delivery state must not become an account-existence oracle."""
    configure_email(monkeypatch)
    signup(env)
    real = env.post("/v1/auth/password/forgot",
                    json={"email": "sam@example.com"}).get_json()
    ghost = env.post("/v1/auth/password/forgot",
                     json={"email": "nobody@example.com"}).get_json()
    assert set(real) == set(ghost), f"response shape differs: {set(real) ^ set(ghost)}"
    assert real["message"] == ghost["message"]
    assert real["email_configured"] == ghost["email_configured"]


# -- real delivery -----------------------------------------------------------

def test_signup_sends_a_verification_email(env, monkeypatch):
    fake = configure_email(monkeypatch)
    from nova_cloud import mailer
    j = signup(env).get_json()
    assert j["email_delivery"]["sent"] is True
    assert j["email_delivery"]["email_provider"] == "resend"
    assert "dev_token" not in j["email_delivery"], "token leaked despite delivery"

    mailer.mailer().flush(timeout=10)
    assert len(fake.messages) == 1
    msg = fake.messages[0]
    assert msg["to"] == "sam@example.com"
    assert "verify" in msg["subject"].lower()
    assert "https://nova.omniel.com.ng/verify?token=" in msg["text"]
    assert msg["from"] == "NOVA <hello@omniel.com.ng>"


def test_the_verification_link_actually_verifies(env, monkeypatch):
    fake = configure_email(monkeypatch)
    from nova_cloud import mailer
    j = signup(env).get_json()
    mailer.mailer().flush(timeout=10)
    _unverify(j["user"]["id"])

    # Pull the token out of the email the user would actually receive.
    text = fake.messages[0]["text"]
    token = text.split("token=")[1].split()[0].strip()

    r = env.post("/v1/auth/email/verify", json={"token": token})
    assert r.status_code == 200

    me = env.get("/v1/auth/me", headers=auth(j["access_token"])).get_json()
    assert me["user"]["email_verified"] is True


def test_password_reset_email_completes_the_real_flow(env, monkeypatch):
    fake = configure_email(monkeypatch)
    from nova_cloud import mailer
    signup(env)
    env.post("/v1/auth/password/forgot", json={"email": "sam@example.com"})
    mailer.mailer().flush(timeout=10)

    reset = [m for m in fake.messages if "reset" in m["subject"].lower()]
    assert reset, "no reset email was sent"
    token = reset[0]["text"].split("token=")[1].split()[0].strip()

    r = env.post("/v1/auth/password/reset",
                 json={"token": token, "password": "a-brand-new-passphrase"})
    assert r.status_code == 200
    ok = env.post("/v1/auth/login", json={
        "email": "sam@example.com", "password": "a-brand-new-passphrase",
        **device("b")})
    assert ok.status_code == 200


def test_a_new_device_sign_in_sends_a_security_notice(env, monkeypatch):
    fake = configure_email(monkeypatch)
    from nova_cloud import mailer
    signup(env)
    mailer.mailer().flush(timeout=10)
    before = len(fake.messages)

    env.post("/v1/auth/login", json={"email": "sam@example.com",
                                     "password": PASSWORD, **device("laptop2")})
    mailer.mailer().flush(timeout=10)
    new = fake.messages[before:]
    assert any("sign-in" in m["subject"].lower() for m in new), \
        "no new-device notice was sent"
    notice = next(m for m in new if "sign-in" in m["subject"].lower())
    assert "machine-laptop2" in notice["text"]


def test_signing_in_on_a_known_device_does_not_spam_the_user(env, monkeypatch):
    fake = configure_email(monkeypatch)
    from nova_cloud import mailer
    d = device("a")
    env.post("/v1/auth/signup", json={"email": "sam@example.com",
                                      "password": PASSWORD, **d})
    mailer.mailer().flush(timeout=10)
    before = len(fake.messages)
    env.post("/v1/auth/login", json={"email": "sam@example.com",
                                     "password": PASSWORD, **d})
    mailer.mailer().flush(timeout=10)
    assert len(fake.messages) == before, "a familiar device triggered an alert"


def test_resend_verification_works_and_is_rate_limited(env, monkeypatch):
    fake = configure_email(monkeypatch)
    from nova_cloud import mailer
    j = signup(env).get_json()
    tok = j["access_token"]
    mailer.mailer().flush(timeout=10)
    _unverify(j["user"]["id"])
    before = len(fake.messages)

    r = env.post("/v1/auth/email/resend", headers=auth(tok))
    assert r.status_code == 200
    mailer.mailer().flush(timeout=10)
    assert len(fake.messages) > before

    codes = [env.post("/v1/auth/email/resend", headers=auth(tok)).status_code
             for _ in range(8)]
    assert 429 in codes, "resend is not rate limited"


def test_resend_is_a_no_op_for_a_verified_account(env, monkeypatch):
    configure_email(monkeypatch)
    tok = signup(env).get_json()["access_token"]     # verified by default
    r = env.post("/v1/auth/email/resend", headers=auth(tok))
    assert r.get_json().get("already_verified") is True


# -- failures ----------------------------------------------------------------

def test_a_provider_outage_does_not_break_signup(env, monkeypatch):
    from nova_cloud import mailer
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("EMAIL_FROM", "NOVA <hello@omniel.com.ng>")
    mailer.reset_mailer()
    monkeypatch.setitem(mailer._PROVIDERS, "resend", FakeProvider(fail=True))

    r = signup(env)
    assert r.status_code == 201, "sign-up failed because email did"
    assert r.get_json()["access_token"]


def test_addresses_are_masked_in_logs():
    from nova_cloud.mailer import _mask
    assert _mask("samuel@example.com") == "sa***@example.com"
    assert _mask("") == "***"
    assert "samuel" not in _mask("samuel@example.com")


def test_no_domain_is_hard_coded_in_the_backend():
    """Every address must come from configuration."""
    import io
    import pathlib
    offenders = []
    for path in pathlib.Path("nova_cloud").glob("*.py"):
        src = io.open(path, encoding="utf-8").read()
        if "omniel.com.ng" in src.lower():
            offenders.append(path.name)
    assert not offenders, f"hard-coded company domain in: {offenders}"


def test_the_config_reads_addresses_from_the_environment(monkeypatch):
    from nova_cloud import mailer
    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("EMAIL_FROM", "NOVA <hello@omniel.com.ng>")
    monkeypatch.setenv("SUPPORT_EMAIL", "support@omniel.com.ng")
    monkeypatch.setenv("ADMIN_EMAIL", "admin@omniel.com.ng")
    cfg = mailer.email_config()
    assert cfg.configured is True
    assert cfg.support_email == "support@omniel.com.ng"
    assert cfg.admin_email == "admin@omniel.com.ng"


def test_from_falls_back_to_the_support_address(monkeypatch):
    from nova_cloud import mailer
    monkeypatch.delenv("EMAIL_FROM", raising=False)
    monkeypatch.setenv("SUPPORT_EMAIL", "support@omniel.com.ng")
    monkeypatch.setenv("NOVA_BRAND", "NOVA")
    cfg = mailer.email_config()
    assert cfg.from_address == "NOVA <support@omniel.com.ng>"


# -- account status ----------------------------------------------------------

def test_a_suspended_account_loses_access(env):
    j = signup(env).get_json()
    from nova_cloud.db import session_scope
    from nova_cloud.models import User, UserStatus
    from nova_cloud.auth_guard import invalidate_auth_cache
    with session_scope() as s:
        s.get(User, j["user"]["id"]).status = UserStatus.SUSPENDED.value
    invalidate_auth_cache()

    r = env.get("/v1/auth/me", headers=auth(j["access_token"]))
    assert r.status_code == 403
    assert env.post("/v1/auth/refresh",
                    json={"refresh_token": j["refresh_token"]}).status_code == 403
    assert env.post("/v1/auth/login", json={
        "email": "sam@example.com", "password": PASSWORD,
        **device("a")}).status_code == 403


def test_suspension_is_distinct_from_disabled():
    from nova_cloud.models import UserStatus
    assert UserStatus.SUSPENDED.value != UserStatus.DISABLED.value
    assert UserStatus.blocks_access(UserStatus.SUSPENDED.value)
    assert UserStatus.blocks_access(UserStatus.DISABLED.value)
    assert not UserStatus.blocks_access(UserStatus.ACTIVE.value)


def test_reset_never_reveals_whether_an_account_exists(env, monkeypatch):
    """The strongest form of the oracle test: with email configured, the two
    responses must be byte-identical."""
    configure_email(monkeypatch)
    signup(env)
    real = env.post("/v1/auth/password/forgot",
                    json={"email": "sam@example.com"}).get_data(as_text=True)
    ghost = env.post("/v1/auth/password/forgot",
                     json={"email": "nobody@example.com"}).get_data(as_text=True)
    assert real == ghost, "the reset response differs for a known address"


def test_production_never_hands_back_a_token(env, monkeypatch):
    """§38: a development affordance must not be reachable in production."""
    import nova_cloud.api_auth as api_auth

    class ProdCfg:
        is_production = True
        require_email_verification = False

    monkeypatch.setitem(env.application.config, "NOVA_CFG", ProdCfg())
    j = env.post("/v1/auth/password/forgot",
                 json={"email": "sam@example.com"}).get_json()
    assert "dev_reset_token" not in j
    body = str(j)
    assert "token" not in body.lower() or "dev" not in body.lower()


# -- error taxonomy (section 50) ---------------------------------------------

def test_a_known_error_code_is_stored_as_itself(env):
    j = signup(env).get_json()
    env.post("/v1/telemetry/events", headers=auth(j["access_token"]), json={
        "events": [{"kind": "error", "code": "MODEL_TIMEOUT",
                    "context": {"provider": "gemini"}}]})
    from nova_cloud.db import session_scope
    from nova_cloud.models import ErrorEvent
    from sqlalchemy import select
    with session_scope() as s:
        row = s.scalar(select(ErrorEvent))
    assert row.code == "MODEL_TIMEOUT"
    assert row.context["provider"] == "gemini"


def test_an_unknown_code_does_not_create_a_new_category(env):
    """One client's typo must not fragment a real problem across rows."""
    j = signup(env).get_json()
    env.post("/v1/telemetry/events", headers=auth(j["access_token"]), json={
        "events": [{"kind": "error", "code": "MODLE_TIMEOTU"}]})
    from nova_cloud.db import session_scope
    from nova_cloud.models import ErrorEvent
    from sqlalchemy import select
    with session_scope() as s:
        row = s.scalar(select(ErrorEvent))
    assert row.code == "UNKNOWN_ERROR"
    # ...but what the client actually said is not lost.
    assert row.context["source"] == "MODLE_TIMEOTU"


def test_email_delivery_failure_is_a_recognised_error_code():
    from nova_cloud.telemetry_sink import ERROR_CODES
    assert "EMAIL_DELIVERY_FAILED" in ERROR_CODES


def test_an_error_context_cannot_carry_private_content(env):
    j = signup(env).get_json()
    env.post("/v1/telemetry/events", headers=auth(j["access_token"]), json={
        "events": [{"kind": "error", "code": "VOICE_ERROR",
                    "context": {"transcript": "something private",
                                "prompt": "also private",
                                "platform": "windows"}}]})
    from nova_cloud.db import session_scope
    from nova_cloud.models import ErrorEvent
    from sqlalchemy import select
    with session_scope() as s:
        row = s.scalar(select(ErrorEvent))
    stored = row.context or {}
    assert "transcript" not in stored and "prompt" not in stored


# -- the pages people actually click (section 9) -----------------------------

def test_the_verification_link_lands_on_a_real_page(env, monkeypatch):
    """The templates point at {APP_BASE_URL}/verify. Without that route every
    verification email leads to a 404, which looks exactly like a broken
    account."""
    fake = configure_email(monkeypatch)
    from nova_cloud import mailer
    j = signup(env).get_json()
    mailer.mailer().flush(timeout=10)
    _unverify(j["user"]["id"])

    token = fake.messages[0]["text"].split("token=")[1].split()[0].strip()
    r = env.get(f"/verify?token={token}")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Email confirmed" in body

    me = env.get("/v1/auth/me", headers=auth(j["access_token"])).get_json()
    assert me["user"]["email_verified"] is True


def test_a_verification_link_works_once(env, monkeypatch):
    fake = configure_email(monkeypatch)
    from nova_cloud import mailer
    j = signup(env).get_json()
    mailer.mailer().flush(timeout=10)
    _unverify(j["user"]["id"])
    token = fake.messages[0]["text"].split("token=")[1].split()[0].strip()

    assert env.get(f"/verify?token={token}").status_code == 200
    again = env.get(f"/verify?token={token}")
    assert again.status_code == 400
    assert "did not work" in again.get_data(as_text=True)


def test_a_bad_verification_token_explains_itself(env):
    r = env.get("/verify?token=not-a-real-token")
    assert r.status_code == 400
    body = r.get_data(as_text=True)
    assert "no longer valid" in body


def test_the_verification_page_escapes_its_message(env):
    """The token comes from a URL; nothing derived from it may reach the page
    unescaped."""
    r = env.get("/verify?token=<script>alert(1)</script>")
    body = r.get_data(as_text=True)
    assert "<script>alert(1)</script>" not in body


def test_the_reset_page_is_served(env):
    r = env.get("/reset?token=anything")
    assert r.status_code == 200
    assert "Reset your NOVA password" in r.get_data(as_text=True)


def test_the_reset_page_refuses_a_missing_token(env):
    r = env.get("/reset")
    assert r.status_code == 400


def test_the_reset_page_never_puts_a_password_in_a_url():
    """The token travels in the URL; the new password must not."""
    import io
    js = io.open("nova_cloud/static/reset.js", encoding="utf-8").read()
    assert "location.search" in js          # token read from the URL
    assert 'method: "POST"' in js           # password sent in the body
    assert "password" in js and "?password=" not in js


def test_health_reports_email_and_distinguishes_unconfigured_from_broken(env):
    """A deployment may legitimately run without email. It is only an outage
    when verification is mandatory and therefore cannot complete."""
    j = env.get("/health").get_json()
    assert "email" in j["checks"]
    assert j["checks"]["email"]["status"] in (
        "healthy", "not_configured", "degraded", "unknown")
