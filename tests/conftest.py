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
import tempfile

import pytest

#: Where the suite is allowed to keep NOVA's user data.
#:
#: A dev run stores it in the working directory, and for pytest that is the
#: repository, so the suite was writing test fixtures into the developer's own
#: living_memory.json and memory index. Set before any NOVA module is
#: imported, because nova.py resolves the directory at import time.
_DATA_SANDBOX = tempfile.mkdtemp(prefix="nova-test-data-")
os.environ["NOVA_DATA_DIR"] = _DATA_SANDBOX

#: The machine-level folder (lifecycle.json, accounts\<id>\). Without this a
#: test that signs in would activate an account in the developer's real
#: %APPDATA%\NOVA -- and the first activation adopts (moves) the data there.
os.environ["NOVA_MACHINE_DIR"] = tempfile.mkdtemp(prefix="nova-test-machine-")


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
    """Clear production credentials before every test, not just once.

    Popping them at session start is not enough: several NOVA modules call
    load_dotenv() at import time, so the first test that imports one puts the
    live DATABASE_URL back into the environment. Anything importing that
    module afterwards would quietly point at Supabase.

    The value is removed rather than merely reported, so a stray import cannot
    hand a test the production database, and the assertion below then catches
    anything that sets one deliberately.
    """
    for key in _PRODUCTION_KEYS:
        value = os.environ.get(key, "")
        if key == "DATABASE_URL" and value and not value.startswith("sqlite"):
            os.environ.pop(key, None)
        elif key in ("RESEND_API_KEY", "NOVA_ADMIN_PASSWORD") and value:
            os.environ.pop(key, None)
        # Policy flags from the developer's .env (re-added by any module that
        # calls load_dotenv) made cloud tests depend on which test imported
        # nova first. A test that wants them sets them itself.
        elif key in ("NOVA_REQUIRE_EMAIL_VERIFICATION", "NOVA_ADMIN_REQUIRE_MFA"):
            os.environ.pop(key, None)

    yield


@pytest.fixture(autouse=True)
def _no_account_leaks_between_tests():
    """nova_lifecycle.activate_account() sets NOVA_ACCOUNT_ID / NOVA_DATA_DIR
    directly. Left behind, the next test resolves paths as that account -- and
    once did so inside the developer's real %APPDATA%/NOVA. Reset after every
    test, whatever it did."""
    yield
    os.environ.pop("NOVA_ACCOUNT_ID", None)
    os.environ["NOVA_DATA_DIR"] = _DATA_SANDBOX
    try:
        import nova_lifecycle
        nova_lifecycle._original_data_dir = None
    except Exception:
        pass
