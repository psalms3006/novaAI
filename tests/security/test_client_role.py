"""Client-side identity: the desktop can read its role, never grant it.

The client-side role system that was in flight when the hardening started
had three defects, each with a test here: the module did not even import
(broken indentation from a hand-applied patch), it attempted to *write*
OWNER onto its own account from the client, and it treated the profile
endpoint as a role-granting channel. Role comes from the backend's session
payload or it does not exist.
"""
from __future__ import annotations

import importlib
import json
import time

import pytest

import nova_account
from nova_account import NovaAccount


def _account_with_session(monkeypatch, tmp_path, user_payload: dict):
    """A NovaAccount whose session holds exactly the given server payload —
    the only channel through which a role may exist client-side."""
    a = NovaAccount(base_url="")
    a._session = {
        "user": user_payload,
        "access_token": "test-token-not-real",
        "access_expires": time.time() + 900,
        "refresh_token": "",
        "last_verified": time.time(),
        "last_activity": time.time(),
    }
    return a


class TestRoleSource:
    def test_role_defaults_to_user(self):
        a = NovaAccount(base_url="")
        assert a.get_user_role() == "USER"

    def test_role_comes_from_server_payload(self, monkeypatch, tmp_path):
        a = _account_with_session(
            monkeypatch, tmp_path,
            {"id": "u1", "email": "dev@example.com", "role": "ADMIN"})
        assert a.get_user_role() == "ADMIN"

    def test_owner_email_alone_confers_nothing(self, monkeypatch, tmp_path):
        """Email equality is identity at most, never authority."""
        a = _account_with_session(
            monkeypatch, tmp_path,
            {"id": "u1", "email": "psalmistasagwara3006@gmail.com"})
        assert a.get_user_role() == "USER"
        assert a._has_owner_role() is False

    def test_unverified_owner_email_confers_nothing(self, monkeypatch, tmp_path):
        a = _account_with_session(
            monkeypatch, tmp_path,
            {"id": "u1", "email": "psalmistasagwara3006@gmail.com",
             "email_verified": False})
        assert a.get_user_role() == "USER"


class TestNoClientSideElevation:
    def test_provision_owner_role_does_not_exist(self):
        """The self-elevation entry point is gone, not merely broken."""
        assert not hasattr(NovaAccount, "provision_owner_role"), \
            "the client must not carry a method that grants itself OWNER"

    def test_update_profile_never_sends_role(self, monkeypatch):
        """Whatever a caller passes, the role field never leaves the client."""
        sent = {}

        def fake_request(method, path, *, body=None, token=None, timeout=8.0):
            sent["method"], sent["path"], sent["body"] = method, path, body
            return {"instance": {}}

        a = _account_with_session(
            monkeypatch, {}, {"id": "u1", "email": "x@example.com"})
        monkeypatch.setattr(a, "_request", fake_request)
        monkeypatch.setattr(a, "ensure_access_token", lambda **kw: "tok")
        a.update_profile(role="OWNER", preferred_name="Bob")
        assert "role" not in (sent["body"] or {})
        assert sent["body"] == {"preferred_name": "Bob"}

    def test_set_user_role_refuses_without_owner(self, monkeypatch):
        a = _account_with_session(
            monkeypatch, {}, {"id": "u1", "email": "x@example.com",
                              "role": "USER"})
        assert a.set_user_role("u2", "ADMIN") is False

    def test_set_user_role_refuses_even_with_owner(self, monkeypatch):
        """There is no backend channel for it yet; a client-side 'yes'
        would be the model or the client deciding. It stays inert."""
        a = _account_with_session(
            monkeypatch, {}, {"id": "u1", "email": "x@example.com",
                              "role": "OWNER"})
        assert a.set_user_role("u2", "ADMIN") is False


class TestAuthRolesModule:
    def test_module_imports(self):
        from agent import auth_roles
        assert callable(auth_roles.get_user_role)

    def test_owner_check_fails_closed_for_user(self, monkeypatch):
        from agent import auth_roles
        monkeypatch.setattr(auth_roles, "get_user_role", lambda: "USER")
        assert auth_roles.has_role("OWNER") is False
        assert auth_roles.has_role("ADMIN") is False

    def test_admin_passes_admin_check_not_owner(self, monkeypatch):
        from agent import auth_roles
        monkeypatch.setattr(auth_roles, "get_user_role", lambda: "ADMIN")
        assert auth_roles.has_role("ADMIN") is True
        assert auth_roles.has_role("OWNER") is False

    def test_invalid_role_name_fails_closed(self, monkeypatch):
        from agent import auth_roles
        monkeypatch.setattr(auth_roles, "get_user_role", lambda: "USER")
        assert auth_roles.has_role("SUPEROWNER") is False

    def test_role_resolution_failure_fails_closed(self, monkeypatch):
        """If role resolution raises, the answer is 'not authorised', never
        'assume the best'."""
        from agent import auth_roles

        def boom():
            raise RuntimeError("session store unreadable")
        monkeypatch.setattr(auth_roles, "get_user_role", boom)
        assert auth_roles.has_role("USER") is False


class TestModuleHealth:
    def test_nova_account_compiles_and_imports(self):
        """The in-flight patch left the module unparseable; the desktop
        client could not start. Import is the regression test."""
        import nova_account as m
        importlib.reload(m)

    def test_no_owner_email_hardcoded_in_client(self):
        """A hardcoded owner email in the client is a self-elevation
        instruction waiting for a lenient backend. Its absence is the
        control."""
        src = open(nova_account.__file__, encoding="utf-8-sig").read()
        assert "psalmistasagwara3006" not in src, \
            "the designated owner's email must live in server provisioning, " \
            "not in the client binary"


class TestSessionIntegrity:
    def test_revoked_epoch_rejected_on_refresh(self, monkeypatch):
        """A 401/403 on refresh is a real sign-out, not an offline blip."""
        from nova_account import AccountError
        a = NovaAccount(base_url="")
        a._session = {
            "user": {"id": "u1", "email": "x@example.com"},
            "access_token": "old", "access_expires": time.time() - 10,
            "refresh_token": "r", "last_verified": time.time(),
            "last_activity": time.time(),
        }

        def refuse(method, path, **kw):
            raise AccountError("revoked", "unauthorised", 401)
        monkeypatch.setattr(a, "_request", refuse)
        tok = a.ensure_access_token()
        assert tok == ""
        assert a.signed_in is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
