"""A failing voice session must say why, rather than showing 'listening'.

These cover the defect reported on 2026-09-10: NOVA's packaged app sat on
"listening" while Gemini rejected the credential six times in ninety seconds.
The microphone was never opened, nothing was ever spoken, and no surface --
interface, transcript, or state -- carried a word of explanation.

Three separate things had to be wrong at once for that to happen, so all three
are pinned here.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


# -- 1. a rejected credential is permanent, not a network blip ---------------

class TestAuthFailureClassification:
    def test_recognises_gemini_key_rejection(self):
        from desk.live_session import LiveManager
        # The exact string Live closes with, taken from the session log.
        assert LiveManager._is_auth_failure(
            "1007 None. API key not valid. Please pass a valid API key.")

    @pytest.mark.parametrize("err", [
        "PERMISSION_DENIED: caller lacks permission",
        "UNAUTHENTICATED: request had invalid credentials",
        "Invalid authentication credentials",
    ])
    def test_recognises_other_auth_rejections(self, err):
        from desk.live_session import LiveManager
        assert LiveManager._is_auth_failure(err)

    @pytest.mark.parametrize("err", [
        "timed out during opening handshake",
        "1006 None. abnormal closure [internal]",
        "sent 1011 (internal error) keepalive ping timeout",
        "[Errno 11001] getaddrinfo failed",
        "503 Service Unavailable",
    ])
    def test_transient_failures_are_not_auth(self, err):
        """Network problems must still retry -- fail-fast is only for credentials."""
        from desk.live_session import LiveManager
        assert not LiveManager._is_auth_failure(err)

    def test_empty_error_is_not_auth(self):
        from desk.live_session import LiveManager
        assert not LiveManager._is_auth_failure("")
        assert not LiveManager._is_auth_failure(None)

    def test_auth_failure_stops_the_retry_loop(self):
        """The reconnect loop must break on auth, not burn six attempts.

        Asserted against the source because driving the real loop needs a live
        websocket; what matters is that the flag is set on an auth error and
        that the loop consults it before backing off.
        """
        src = (ROOT / "desk" / "live_session.py").read_text(encoding="utf-8")
        assert "self._auth_rejected = True" in src, \
            "an auth failure must set the fail-fast flag"
        loop = src.split("while self._should_reconnect():", 1)[1]
        gate = loop.index("if self._auth_rejected:")
        backoff = loop.index("await asyncio.sleep(delay)")
        assert gate < backoff, \
            "the auth check must come before the backoff, or a rejected key " \
            "is still retried six times"

    def test_auth_failure_is_reset_per_session(self):
        """A key fixed in Settings must be retried on the next start()."""
        src = (ROOT / "desk" / "live_session.py").read_text(encoding="utf-8")
        start = src.split("def start(self)", 1)[1].split("def stop(self)", 1)[0]
        assert "self._auth_rejected = False" in start, \
            "start() must clear the flag, or voice stays dead until restart"


# -- 2. the failure carries a message a person can act on -------------------

class TestActionableMessage:
    def test_auth_giveup_publishes_plain_language(self):
        src = (ROOT / "desk" / "live_session.py").read_text(encoding="utf-8")
        block = src.split("if self._auth_rejected:", 1)[1][:900]
        assert 'fatal="auth"' in block
        assert "message=" in block, "the give-up event must carry a message"
        assert "Settings" in block, \
            "the message must tell the user where to fix it"

    def test_mic_failure_is_published_not_swallowed(self):
        """Both mic failure paths must publish; neither may return in silence."""
        src = (ROOT / "desk" / "live_session.py").read_text(encoding="utf-8")
        mic = src.split("def _start_mic(self)", 1)[1].split("def _stop_mic", 1)[0]

        assert "mic_unavailable" in mic, \
            "a missing audio backend must be reported"
        assert "mic_open_failed" in mic, \
            "a microphone that will not open must be reported"
        assert mic.count("self._publish(") >= 3, \
            "each mic failure path needs an error event and a state event"
        # The Windows case that produces a silent dead mic.
        assert "privacy" in mic.lower(), \
            "the message should point at Windows microphone privacy settings"


# -- 3. the interface never claims 'listening' on its own authority ---------

class TestInterfaceHonesty:
    @pytest.fixture(scope="class")
    def runtime(self):
        return (ROOT / "desk" / "ui" / "src" / "nova" / "runtime.tsx").read_text(encoding="utf-8")

    def test_start_does_not_assert_listening(self, runtime):
        """/api/live/start returns when a thread spawns, which proves nothing.

        This is the line that made a dead session look like a live one.
        """
        start = runtime.split("const startVoice = useCallback", 1)[1].split("const stopVoice", 1)[0]
        for claimed in ("'listening'", "'ready'", "'connected'"):
            assert f"setVoiceState({claimed})" not in start, \
                "the window must not claim the session is up before the backend says so"
        assert "VOICE_READY_TIMEOUT_MS" in start, \
            "a session that never reports ready must time out visibly"

    def test_backend_state_still_drives_the_orb(self, runtime):
        """Fixing the above must not disconnect the real state events."""
        handler = runtime.split("case 'state': {", 1)[1][:600]
        assert "setVoiceState(s)" in handler, "the backend's own state must reach the window"

    def test_error_state_is_said_not_just_coloured(self, runtime):
        handler = runtime.split("case 'state': {", 1)[1][:1400]
        err = handler.split("if (s === 'error')", 1)[1][:400]
        assert "ev.message" in err and "setVoiceError" in err, \
            "a fatal voice error must be said, not just coloured red"
        assert "phase === 'error' && voiceError ? voiceError" in runtime, \
            "the error text must reach what the window shows"

    def test_ready_watchdog_is_cleared_on_success(self, runtime):
        handler = runtime.split("case 'state': {", 1)[1][:700]
        assert "VOICE_UP.has(s) && readyTimer.current" in handler
        assert "clearTimeout(readyTimer.current)" in handler, \
            "a healthy session must cancel the watchdog or it fires anyway"


# -- 4. the credential itself ----------------------------------------------

class TestCredentialClassification:
    def test_durable_api_key(self):
        from desk.creds import classify_credential
        info = classify_credential("AIza" + "x" * 35)
        assert info["kind"] == "api_key"
        assert info["durable"] is True
        assert info["warning"] == ""

    def test_aistudio_key_is_durable(self):
        """`AQ.Ab8…` is a current AI Studio key, not a short-lived token.

        Verified 2026-09-10 against the live API: such a key connected to
        Gemini Live in 3.6 s. An earlier revision of the classifier warned
        that these expire within the hour, which was wrong -- it would have
        nagged every user holding a perfectly good key, and pointed diagnosis
        at expiry when the real defect was a truncated paste.
        """
        from desk.creds import classify_credential
        info = classify_credential("AQ.Ab8" + "y" * 47)
        assert info["kind"] == "api_key_aistudio"
        assert info["durable"] is True
        assert info["warning"] == ""

    def test_truncated_key_is_identified(self):
        """The real defect: an AQ. key that lost its prefix in the paste.

        50 characters beginning 'Ab8' -- 53 minus the missing 'AQ.'. Gemini
        rejects it with "API key not valid", which names the key rather than
        the truncation, so it reads as an entirely wrong key.
        """
        from desk.creds import classify_credential
        info = classify_credential("Ab8RN6" + "z" * 44)
        assert info["kind"] == "truncated_key"
        assert info["durable"] is False
        assert "AQ." in info["warning"]

    def test_a_full_key_and_its_truncation_are_distinguished(self):
        """The two differ by three characters; the diagnosis differs entirely."""
        from desk.creds import classify_credential
        full = "AQ.Ab8RN6" + "w" * 44
        assert classify_credential(full)["durable"] is True
        assert classify_credential(full[3:])["durable"] is False

    def test_empty_credential(self):
        from desk.creds import classify_credential
        assert classify_credential("")["kind"] == "none"
        assert classify_credential(None)["kind"] == "none"

    def test_classification_never_returns_the_secret(self):
        from desk.creds import classify_credential
        secret = "AQ.Ab8SUPERSECRETVALUE" + "q" * 31
        blob = repr(classify_credential(secret))
        assert "SUPERSECRET" not in blob
        assert secret not in blob


class TestDoctorSafety:
    def test_doctor_never_prints_a_whole_credential(self):
        """The diagnostic is meant to be pasted into a chat for help."""
        src = (ROOT / "tools" / "voice_doctor.py").read_text(encoding="utf-8")
        cred = src.split("def check_credential", 1)[1].split("def check_live", 1)[0]
        assert "key[:4]" in cred, "only a short prefix may be shown"
        assert "{key}" not in cred, "the full credential must never be printed"
