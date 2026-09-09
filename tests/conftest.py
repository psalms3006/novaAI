"""Keep the suite away from production.

A .env holding the live DATABASE_URL and RESEND_API_KEY sits in the repository
root for the running application. If pytest inherited it, the suite would
write test accounts into Supabase and send real email through Resend, and the
first sign of the mistake would be production data.

Every credential is therefore cleared before any test runs. Fixtures set their
own throwaway values afterwards.
"""
from __future__ import annotations

import os

import pytest

_PRODUCTION_KEYS = (
    "DATABASE_URL",
    "NOVA_SECRET_KEY", "NOVA_ADMIN_SECRET_KEY",
    "EMAIL_PROVIDER", "RESEND_API_KEY",
    "SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD",
    "EMAIL_FROM", "SUPPORT_EMAIL", "ADMIN_EMAIL", "APP_BASE_URL",
    "NOVA_ENV", "NOVA_ADMIN_PASSWORD",
    "NOVA_REQUIRE_EMAIL_VERIFICATION", "NOVA_ADMIN_REQUIRE_MFA",
    "NOVA_CLOUD_URL",
)


@pytest.fixture(autouse=True, scope="session")
def _isolate_from_production():
    saved = {k: os.environ.pop(k, None) for k in _PRODUCTION_KEYS}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


@pytest.fixture(autouse=True)
def _no_live_database():
    """A test that reaches a non-local database is a bug, not a slow test."""
    url = os.environ.get("DATABASE_URL", "")
    if url and not url.startswith("sqlite"):
        pytest.fail(f"a test tried to use a non-SQLite database: "
                    f"{url.split('://')[0]}://...")
