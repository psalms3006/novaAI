"""Development conveniences must not be reachable in production.

Every check here corresponds to something that is genuinely useful while
building and genuinely dangerous once real users exist. They are tests rather
than a checklist because a checklist is not run.
"""
from __future__ import annotations

import io
import pathlib
import re

import pytest

SOURCES = sorted(pathlib.Path("nova_cloud").glob("*.py"))


@pytest.fixture()
def live(monkeypatch):
    """A running app with one user and one super admin."""
    import os
    import tempfile
    import uuid
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + os.path.join(tmp, "h.db"))
    monkeypatch.setenv("NOVA_ENV", "test")
    monkeypatch.setenv("NOVA_SECRET_KEY", "u-" + uuid.uuid4().hex)
    monkeypatch.setenv("NOVA_ADMIN_SECRET_KEY", "a-" + uuid.uuid4().hex)
    monkeypatch.setenv("NOVA_ADMIN_REQUIRE_MFA", "0")

    from nova_cloud import config as cfgmod, db
    from nova_cloud.auth_guard import invalidate_auth_cache
    cfgmod.reset_config()
    db.reset_engine()
    invalidate_auth_cache()

    from nova_cloud.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    pw = "a-long-enough-passphrase"
    user = client.post("/v1/auth/signup", json={
        "email": "hardening@nova.test", "password": pw, "display_name": "H",
        "device_id": "hd-1", "device_secret": "hs-1",
        "platform": "windows"}).get_json()

    from nova_cloud import security as sec
    from nova_cloud.db import session_scope
    from nova_cloud.models import AdminUser
    with session_scope() as s:
        s.add(AdminUser(email="root@nova.test",
                        password_hash=sec.hash_password(pw),
                        role="SUPER_ADMIN", mfa_enabled=False))
    admin_token = client.post("/admin/api/auth/login", json={
        "email": "root@nova.test", "password": pw}).get_json()["access_token"]

    yield client, user, admin_token
    db.reset_engine()
    cfgmod.reset_config()



def src(path) -> str:
    return io.open(path, encoding="utf-8").read()


# -- secrets -----------------------------------------------------------------

def test_no_hard_coded_secrets_in_the_backend():
    """A literal key, password or token committed to the repository."""
    patterns = [
        re.compile(r"""api[_-]?key\s*=\s*["'][A-Za-z0-9_\-]{16,}["']""", re.I),
        re.compile(r"""password\s*=\s*["'][^"'\s]{8,}["']"""),
        re.compile(r"""secret\s*=\s*["'][A-Za-z0-9_\-]{16,}["']""", re.I),
        re.compile(r"""["']re_[A-Za-z0-9]{20,}["']"""),          # Resend key
        re.compile(r"""["']sk-[A-Za-z0-9]{20,}["']"""),          # generic
        re.compile(r"""["']eyJ[A-Za-z0-9_\-]{20,}\."""),         # a real JWT
    ]
    offenders = []
    for path in SOURCES:
        body = src(path)
        for pat in patterns:
            for hit in pat.finditer(body):
                line = body[:hit.start()].count("\n") + 1
                offenders.append(f"{path.name}:{line}")
    assert not offenders, f"possible hard-coded secret at {offenders}"


def test_the_example_env_contains_no_real_values():
    body = src(pathlib.Path("nova_cloud/.env.example"))
    assert "re_" not in body.replace("RESEND_API_KEY=", "")
    for line in body.splitlines():
        if line.startswith("NOVA_SECRET_KEY=") or line.startswith("NOVA_ADMIN_SECRET_KEY="):
            value = line.split("=", 1)[1]
            assert "replace-me" in value, f"example env has a real-looking key: {line}"


def test_no_default_admin_credentials_anywhere():
    """Section 10: there must be no admin/admin, no baked-in password."""
    banned = re.compile(r"""(admin|root)\s*[:/]\s*(admin|password|changeme|1234)""",
                        re.I)
    for path in SOURCES:
        assert not banned.search(src(path)), f"default credentials in {path.name}"

    manage = src(pathlib.Path("nova_cloud/manage.py"))
    # The password must come from a prompt or the environment, never an
    # argument that would land in shell history and process listings.
    assert "getpass" in manage
    assert "--password" not in manage, \
        "create-admin accepts a password argument; it would leak into history"


def test_admin_creation_requires_an_explicit_role_and_strong_password():
    manage = src(pathlib.Path("nova_cloud/manage.py"))
    assert "check_password_policy" in manage
    assert "PERMISSIONS" in manage, "role is not validated against the RBAC table"


# -- production guards -------------------------------------------------------

def test_production_refuses_generated_signing_keys(monkeypatch):
    monkeypatch.setenv("NOVA_ENV", "production")
    monkeypatch.delenv("NOVA_SECRET_KEY", raising=False)
    monkeypatch.delenv("NOVA_ADMIN_SECRET_KEY", raising=False)
    from nova_cloud import config as cfgmod
    cfgmod.reset_config()
    with pytest.raises(RuntimeError):
        cfgmod.config()
    cfgmod.reset_config()


def test_every_dev_token_is_gated_on_not_production():
    """Any response field handing back a token must be behind is_production."""
    body = src(pathlib.Path("nova_cloud/api_auth.py"))
    for match in re.finditer(r"dev_\w*token", body):
        window = body[max(0, match.start() - 600):match.start()]
        assert "is_production" in window, (
            f"a dev token near offset {match.start()} is not gated on "
            "cfg.is_production")


def test_console_email_is_never_a_production_provider():
    """`console` prints messages to the log; it must be an explicit choice."""
    mailer = src(pathlib.Path("nova_cloud/mailer.py"))
    default = re.search(r'_s\("EMAIL_PROVIDER",\s*"(\w+)"\)', mailer)
    assert default and default.group(1) == "none", \
        "EMAIL_PROVIDER defaults to something that could imply delivery"


def test_debug_mode_is_never_enabled_in_source():
    for path in SOURCES:
        body = src(path)
        assert "debug=True" not in body, f"{path.name} enables Flask debug"
        assert "DEBUG = True" not in body


def test_security_headers_are_set_on_every_response():
    app_src = src(pathlib.Path("nova_cloud/app.py"))
    for header in ("X-Content-Type-Options", "X-Frame-Options",
                   "Content-Security-Policy", "Referrer-Policy",
                   "Cache-Control"):
        assert header in app_src, f"{header} is not set"
    assert "Strict-Transport-Security" in app_src


def test_internal_errors_are_not_leaked_to_clients():
    app_src = src(pathlib.Path("nova_cloud/app.py"))
    handler = app_src[app_src.index("def _500("):]
    assert "internal_error" in handler
    assert "str(e)" not in handler.split("return")[0], \
        "the 500 handler puts exception text in the response"


def test_no_test_or_seed_accounts_are_created_by_the_backend():
    """Section 38: no fake users, ever, from the running application."""
    banned = ("example.com", "demo@", "seed_users", "fake_user",
              "INSERT INTO users")
    offenders = []
    for path in SOURCES:
        body = src(path)
        for token in banned:
            if token in body:
                offenders.append((path.name, token))
    assert not offenders, f"backend references test/demo accounts: {offenders}"


# -- privacy -----------------------------------------------------------------

def test_no_response_body_ever_contains_a_password_hash(live):
    """Checked against real responses, not by grepping for the word.

    A source scan flags `verify_password(user.password_hash, ...)` and misses
    an actual leak in a dict built somewhere else. Hitting the endpoints and
    inspecting the bytes cannot be fooled either way.
    """
    client, user, admin_token = live
    paths = [
        ("get", "/v1/auth/me", {"Authorization": f"Bearer {user['access_token']}"}),
        ("get", "/v1/devices", {"Authorization": f"Bearer {user['access_token']}"}),
        ("get", "/v1/sync/profile", {"Authorization": f"Bearer {user['access_token']}"}),
        ("get", "/admin/api/users", {"Authorization": f"Bearer {admin_token}"}),
        ("get", f"/admin/api/users/{user['user']['id']}",
         {"Authorization": f"Bearer {admin_token}"}),
        ("get", "/admin/api/admins", {"Authorization": f"Bearer {admin_token}"}),
        ("get", "/admin/api/audit", {"Authorization": f"Bearer {admin_token}"}),
    ]
    for method, path, headers in paths:
        body = getattr(client, method)(path, headers=headers).get_data(as_text=True)
        assert "password_hash" not in body, f"{path} leaked the field name"
        assert "$argon2" not in body, f"{path} leaked a password hash"
        assert "totp_secret" not in body, f"{path} leaked a TOTP seed"
        assert "secret_hash" not in body, f"{path} leaked a device secret hash"
        assert "refresh_hash" not in body, f"{path} leaked a refresh token hash"


def test_logging_never_includes_credentials():
    banned = re.compile(r"""log\.\w+\([^)]*\b(password|api_key|secret|token)\b""",
                        re.I)
    offenders = []
    for path in SOURCES:
        for hit in banned.finditer(src(path)):
            snippet = hit.group(0)
            # Naming a *field* is fine; logging its value is not.
            if "%s" in snippet or "{" in snippet or "+" in snippet:
                offenders.append((path.name, snippet[:60]))
    assert not offenders, f"credentials may reach the log: {offenders}"


def test_the_schema_has_no_column_for_conversation_content():
    """The privacy boundary, asserted against the actual columns.

    Reading the source would match the docstring that promises this, which
    would make the test pass for the wrong reason. The table metadata is the
    only thing that decides what can be stored.
    """
    from nova_cloud.models import Base
    banned = {"transcript", "transcripts", "conversation", "conversations",
              "utterance", "prompt", "completion", "audio", "recording",
              "message", "messages", "content", "text", "body"}
    # Matched on name tokens, not substrings: "context" is a legitimate
    # sanitised metadata column and contains the letters of "text".
    offenders = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            tokens = set(column.name.lower().split("_"))
            if tokens & banned:
                offenders.append(f"{table.name}.{column.name}")
    assert not offenders, f"schema can store private content: {offenders}"


def test_gitignore_excludes_local_secrets():
    body = io.open(".gitignore", encoding="utf-8").read()
    assert ".env" in body, ".env is not ignored"


def test_no_env_file_with_real_values_is_tracked():
    import subprocess
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True)
    tracked = set(out.stdout.split())
    for path in tracked:
        base = path.rsplit("/", 1)[-1]
        if base == ".env" or (base.startswith(".env") and "example" not in base
                              and "template" not in base):
            pytest.fail(f"a real .env file is tracked in git: {path}")
