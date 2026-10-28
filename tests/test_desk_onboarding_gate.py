"""Onboarding isolation: the assistant does not exist until setup is done.

The rule under test: a person who has signed in but not finished
verification and onboarding gets the setup screens and *nothing else* —
no brain, no listener, no chat — no matter which route they try. The gate
lives in `nova_runtime.assistant_allowed()` and every entry point asks it:
launch, sign-in binding, voice start, chat, and onboarding completion.
"""
from __future__ import annotations

import time

import pytest

import nova_runtime
from nova_runtime import assistant_allowed


class FakeAccount:
    """Just enough of NovaAccount for the gate's decisions."""

    def __init__(self, *, configured=True, signed_in=True,
                 verification_required=True, email_verified=False,
                 instance=None):
        self.configured = configured
        self._signed_in = signed_in
        self.status = lambda: {
            "signed_in": self._signed_in,
            "verification_required": verification_required,
            "email_verified": email_verified,
        }
        self.cached_instance = lambda: dict(instance or {})
        self.signed_in = self._signed_in
        self.user_id = "u-1" if self._signed_in else ""


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Keep the singleton honest between tests."""
    # The enforcement gate only applies when the launcher armed it (a build
    # with an account server). These suites exercise that enforcement, so
    # they arm it the way nova_desktop_app does.
    monkeypatch.setenv("NOVA_AUTH_GATE", "1")
    import nova_account
    monkeypatch.setattr(nova_account, "account", lambda: FakeAccount())
    # tests patch nova_runtime's view through the same module
    monkeypatch.setattr("nova_runtime.assistant_allowed", assistant_allowed,
                        raising=False)
    yield


def _patch_acct(monkeypatch, **kw):
    import nova_account
    acct = FakeAccount(**kw)
    monkeypatch.setattr(nova_account, "account", lambda: acct)
    return acct


def _lifecycle_app(monkeypatch, acct):
    """The real desk lifecycle payload, on a bare Flask app."""
    from flask import Flask
    from desk import account_api
    import nova_account
    monkeypatch.setattr(nova_account, "account", lambda: acct)
    app = Flask(__name__)
    account_api.register(app, lambda fn: fn, {})
    return app


class _RefusingAccount:
    """A backend that refuses every authenticated route until verification."""

    configured = True
    display_name = "Sam"

    def status(self):
        # Deliberately *not* flagged: this is the stale-server shape that sent
        # a real user to the profile step and then blocked them there.
        return {"signed_in": True, "verification_required": False,
                "email_verified": False, "online": True}

    def cached_instance(self):
        return {}

    def instance(self):
        import nova_account
        raise nova_account.AccountError(
            "Verify your email address to continue.", "email_unverified", 403)


def test_lifecycle_sends_an_unverified_account_to_the_verify_step(monkeypatch):
    j = _lifecycle_app(monkeypatch, _RefusingAccount()).test_client() \
        .get("/api/lifecycle").get_json()
    assert j["next"] == "verify_email", \
        "the backend refused with 'verify', so setup must stop at verification"


def test_lifecycle_does_not_invent_a_verify_step(monkeypatch):
    """An unrelated backend failure must not be reported as a verification wall."""
    class Acct(_RefusingAccount):
        def instance(self):
            import nova_account
            raise nova_account.AccountError("Server error", "internal_error", 500)

    j = _lifecycle_app(monkeypatch, Acct()).test_client() \
        .get("/api/lifecycle").get_json()
    assert j["next"] == "profile"


# ── the gate itself ──────────────────────────────────────────────────────────

class TestGateDecisions:
    def test_unconfigured_build_is_allowed(self, monkeypatch):
        _patch_acct(monkeypatch, configured=False)
        assert assistant_allowed() == (True, "")

    def test_unsigned_in_blocked(self, monkeypatch):
        _patch_acct(monkeypatch, signed_in=False)
        ok, stage = assistant_allowed()
        assert not ok and stage == "signin"

    def test_unverified_blocked(self, monkeypatch):
        _patch_acct(monkeypatch, signed_in=True, email_verified=False,
                    instance={"onboarding_completed": False})
        ok, stage = assistant_allowed()
        assert not ok and stage == "verify_email"

    def test_verified_but_unonboarded_blocked(self, monkeypatch):
        _patch_acct(monkeypatch, email_verified=True,
                    instance={"onboarding_completed": False})
        ok, stage = assistant_allowed()
        assert not ok and stage == "onboarding"

    def test_fully_set_up_allowed(self, monkeypatch):
        _patch_acct(monkeypatch, email_verified=True,
                    instance={"onboarding_completed": True})
        assert assistant_allowed() == (True, "")

    def test_empty_instance_cache_does_not_lock_out_existing_users(
            self, monkeypatch):
        """A long-standing account whose session predates the instance cache
        must not be held at the gate forever."""
        _patch_acct(monkeypatch, email_verified=True, instance=None)
        assert assistant_allowed() == (True, "")

    def test_no_verification_requirement_means_no_verify_gate(
            self, monkeypatch):
        """A backend that does not require verification cannot be turned
        into a lockout by the client guessing."""
        _patch_acct(monkeypatch, verification_required=False,
                    email_verified=False,
                    instance={"onboarding_completed": True})
        assert assistant_allowed() == (True, "")


# ── the entry points ask the gate ────────────────────────────────────────────

class TestEntryPointGating:
    def _bridge_client(self, monkeypatch):
        """The real bridge app with the token check fixed for tests."""
        import desk.bridge as bridge
        bridge.run_token = "test-desk-token"
        bridge.app.config["TESTING"] = True
        return bridge.app.test_client()

    def _headers(self):
        return {"X-NOVA-Desk": "test-desk-token"}

    def test_voice_start_refused_before_setup(self, monkeypatch):
        client = self._bridge_client(monkeypatch)
        _patch_acct(monkeypatch, email_verified=False,
                    instance={"onboarding_completed": False})
        r = client.post("/api/voice/start", headers=self._headers())
        assert r.status_code == 403
        j = r.get_json()
        assert j["error"] == "setup_incomplete"
        assert j["stage"] == "verify_email"

    def test_chat_refused_before_setup(self, monkeypatch):
        client = self._bridge_client(monkeypatch)
        _patch_acct(monkeypatch, email_verified=True,
                    instance={"onboarding_completed": False})
        r = client.post("/api/chat", headers=self._headers(),
                        json={"message": "hello", "conversation_id": "c1"})
        assert r.status_code == 403
        assert r.get_json()["stage"] == "onboarding"

    def test_voice_start_allowed_after_setup(self, monkeypatch):
        client = self._bridge_client(monkeypatch)
        _patch_acct(monkeypatch, email_verified=True,
                    instance={"onboarding_completed": True})
        # Audio may or may not exist in CI; the gate decision is what we
        # assert: NOT a 403 setup_incomplete.
        r = client.post("/api/voice/start", headers=self._headers())
        assert not (r.status_code == 403 and
                    r.get_json().get("error") == "setup_incomplete")

    def test_chat_allowed_after_setup(self, monkeypatch):
        client = self._bridge_client(monkeypatch)
        _patch_acct(monkeypatch, email_verified=True,
                    instance={"onboarding_completed": True})
        # The brain is not running in tests, so a full chat would fail
        # downstream; the gate decision is what must not block.
        r = client.post("/api/chat", headers=self._headers(),
                        json={"message": "hello", "conversation_id": "c1"})
        assert not (r.status_code == 403 and
                    r.get_json().get("error") == "setup_incomplete")


# ── bind_signed_in_account defers the brain ──────────────────────────────────

class TestSignInBinding:
    def _bind(self, monkeypatch, started, **acct_kw):
        started_calls = []
        monkeypatch.setattr(nova_runtime, "start_brain",
                            lambda: started_calls.append(1))
        _patch_acct(monkeypatch, **acct_kw)
        import nova_lifecycle
        monkeypatch.setattr(nova_lifecycle, "activate_account",
                            lambda uid: {"adopted": []})
        import desk.account_api as account_api
        account_api._NO_RELAUNCH = True
        account_api.bind_signed_in_account(
            __import__("nova_account").account())
        return started_calls

    def test_verified_onboarded_starts_brain(self, monkeypatch):
        calls = self._bind(monkeypatch, started=True, email_verified=True,
                           instance={"onboarding_completed": True})
        assert calls, "setup is done; the assistant should start"

    def test_unverified_does_not_start_brain(self, monkeypatch):
        calls = self._bind(monkeypatch, started=True, email_verified=False,
                           instance={"onboarding_completed": False})
        assert calls == [], "an unverified account got a live assistant"

    def test_unonboarded_does_not_start_brain(self, monkeypatch):
        calls = self._bind(monkeypatch, started=True, email_verified=True,
                           instance={"onboarding_completed": False})
        assert calls == []

    def test_auth_gate_rearmed_when_deferred(self, monkeypatch):
        """The window's health endpoint uses NOVA_AUTH_GATE to know it is
        showing setup screens; deferral must rearm it."""
        import os
        self._bind(monkeypatch, started=True, email_verified=False,
                   instance={"onboarding_completed": False})
        assert os.environ.get("NOVA_AUTH_GATE") == "1"


# ── completion starts the assistant exactly once ────────────────────────────

class TestCompletionStartsAssistant:
    def test_start_brain_is_idempotent(self, monkeypatch):
        """Whatever repeats -- refresh, double POST, restart -- cannot make
        two brains."""
        _patch_acct(monkeypatch, email_verified=True,
                    instance={"onboarding_completed": True})
        # nova_runtime.start_brain guards with _state["started"]; simulate
        # by calling twice against a stubbed import of nova.
        real_import = __import__
        calls = []

        class _NovaStub:
            TOOL_DECLARATIONS = []
            run_desk_server = None

            @staticmethod
            def main():
                calls.append(1)

        import sys
        monkeypatch.setitem(sys.modules, "nova", _NovaStub)
        assert nova_runtime.start_brain() is True
        assert nova_runtime.start_brain() is False, "second start accepted"
        # Let the thread run; the stub main records one entry.
        time.sleep(0.2)
        assert len(calls) <= 1


# ── the window knows it is showing setup, not an assistant ───────────────────

class TestHealthReportsGate:
    def test_auth_required_flag_reflects_the_gate(self, monkeypatch):
        import os
        client = TestEntryPointGating()._bridge_client(monkeypatch)
        _patch_acct(monkeypatch, email_verified=False,
                    instance={"onboarding_completed": False})
        monkeypatch.setenv("NOVA_AUTH_GATE", "1")
        # A previous test's stubbed brain must not leak into this one.
        monkeypatch.setattr(nova_runtime, "_state",
                            {"started": False, "ready": False, "error": "",
                             "started_at": 0.0})
        import desk.bridge as bridge
        monkeypatch.setattr(bridge, "_brain_ready", False, raising=False)
        r = client.get("/api/health")
        j = r.get_json()
        assert j["auth_required"] is True
        assert j["brain_ready"] is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
