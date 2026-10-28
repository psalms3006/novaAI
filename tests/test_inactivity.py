"""Tests for inactivity tracking and session expiration (local logic only)."""
from __future__ import annotations

import os
import tempfile
import time
import uuid

import pytest

import nova_account
import nova_lifecycle
import nova_secure_store as store

# Strong password that passes the policy check
PASSWORD = "StrongPass123!"


@pytest.fixture()
def clean_env(monkeypatch):
    """Isolate test from production environment."""
    # Create a temporary machine directory
    tmp = tempfile.mkdtemp(prefix="nova-test-inactivity-")
    monkeypatch.setenv("NOVA_MACHINE_DIR", tmp)
    monkeypatch.setenv("NOVA_DATA_DIR", os.path.join(tmp, "suite-sandbox"))
    monkeypatch.delenv("NOVA_ACCOUNT_ID", raising=False)

    # Clear secure store
    store.delete_secret("account_session")
    store.delete_secret("device_identities")
    store.delete_secret("installation_identity")

    # Reset modules
    import importlib
    import nova_account
    import nova_lifecycle
    importlib.reload(nova_account)
    importlib.reload(nova_lifecycle)

    yield

    # Cleanup
    store.delete_secret("account_session")
    store.delete_secret("device_identities")
    store.delete_secret("installation_identity")


def _setup_local_account(clean_env):
    """Create a local account session without backend calls."""
    nova_lifecycle.ensure_installation("1.0.0")
    acct_id = uuid.uuid4().hex[:12]
    info = nova_lifecycle.activate_account(acct_id)

    # Create a mock session directly in the secure store
    # This simulates a signed-in account without backend calls
    session = {
        "user": {
            "id": acct_id,
            "email": "test@example.com",
            "display_name": "Test User",
            "email_verified": True,
        },
        "device": {
            "id": str(uuid.uuid4()),
            "name": "Test Device",
        },
        "access_token": "mock_access_token",
        "refresh_token": "mock_refresh_token",
        "offline_grace_s": 30 * 86400,
        "inactivity_threshold_s": 30 * 86400,
        "last_verified": time.time(),
        "last_activity": time.time(),
        "verification_required": False,
        "email_delivery": {},
    }
    import nova_secure_store as store
    store.set_json("account_session", session)

    # Reload account module to pick up the session
    import importlib
    import nova_account
    importlib.reload(nova_account)

    acct = nova_account.account()
    return acct


def test_inactivity_expired_after_threshold(clean_env):
    """Session expires after inactivity threshold."""
    acct = _setup_local_account(clean_env)

    # Verify initially signed in
    assert acct.signed_in is True
    status = acct.status()
    assert status["inactivity_expired"] is False
    assert status["grace_expired"] is False

    # Manually set last_activity to be old (expired)
    threshold = 1  # 1 second for testing
    acct._session["inactivity_threshold_s"] = threshold
    acct._session["last_activity"] = time.time() - 10  # 10 seconds ago
    store.set_json("account_session", acct._session)

    # Reload account to pick up changes
    import importlib
    importlib.reload(nova_account)
    acct2 = nova_account.account()

    # Should be expired due to inactivity
    assert acct2.signed_in is False
    status = acct2.status()
    assert status["inactivity_expired"] is True


def test_inactivity_not_expired_with_recent_activity(clean_env):
    """Session stays valid with recent activity."""
    acct = _setup_local_account(clean_env)

    assert acct.signed_in is True

    # Update activity to now
    acct.update_activity()

    # Reload
    import importlib
    importlib.reload(nova_account)
    acct2 = nova_account.account()

    assert acct2.signed_in is True
    status = acct2.status()
    assert status["inactivity_expired"] is False


def test_update_activity_updates_timestamp(clean_env):
    """update_activity() updates last_activity timestamp."""
    acct = _setup_local_account(clean_env)

    before = time.time()
    acct.update_activity()
    after = time.time()

    status = acct.status()
    last_activity = status["last_activity"]
    assert last_activity is not None
    assert before <= last_activity <= after


def test_inactivity_threshold_configurable(clean_env):
    """Inactivity threshold can be set per session."""
    acct = _setup_local_account(clean_env)

    # Check default threshold
    status = acct.status()
    assert status["inactivity_threshold_s"] == 30 * 86400  # 30 days default

    # Custom threshold
    acct._session["inactivity_threshold_s"] = 3600  # 1 hour
    store.set_json("account_session", acct._session)

    import importlib
    importlib.reload(nova_account)
    acct2 = nova_account.account()
    status = acct2.status()
    assert status["inactivity_threshold_s"] == 3600


def test_inactivity_expired_reflected_in_status(clean_env):
    """inactivity_expired field correctly reported in status()."""
    acct = _setup_local_account(clean_env)

    # Active session
    status = acct.status()
    assert status["inactivity_expired"] is False

    # Expire via old last_activity
    acct._session["inactivity_threshold_s"] = 1
    acct._session["last_activity"] = time.time() - 10
    store.set_json("account_session", acct._session)

    import importlib
    importlib.reload(nova_account)
    acct2 = nova_account.account()

    status = acct2.status()
    assert status["inactivity_expired"] is True
    assert status["signed_in"] is False


def test_signed_in_false_when_inactivity_expired(clean_env):
    """signed_in property returns False when inactivity expired."""
    acct = _setup_local_account(clean_env)

    assert acct.signed_in is True

    # Expire via inactivity
    acct._session["inactivity_threshold_s"] = 1
    acct._session["last_activity"] = time.time() - 10
    store.set_json("account_session", acct._session)

    import importlib
    importlib.reload(nova_account)
    acct2 = nova_account.account()

    assert acct2.signed_in is False


def test_grace_expired_takes_precedence(clean_env):
    """grace_expired takes precedence over inactivity for signed_in."""
    acct = _setup_local_account(clean_env)

    # Expire both grace and inactivity
    acct._session["offline_grace_s"] = 1
    acct._session["last_verified"] = time.time() - 10
    acct._session["inactivity_threshold_s"] = 1
    acct._session["last_activity"] = time.time() - 10
    store.set_json("account_session", acct._session)

    import importlib
    importlib.reload(nova_account)
    acct2 = nova_account.account()

    status = acct2.status()
    assert status["grace_expired"] is True
    assert status["inactivity_expired"] is True
    assert acct2.signed_in is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])