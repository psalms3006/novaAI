"""The welcome screen must not treat "I don't know yet" as "new user".

Reported after entering the Gemini key several times: NOVA kept opening the
first-run screen as though the app had never been set up. The key was stored
correctly every time -- `desk.creds.resolve()` reported mode "byok",
onboarded True, has_credential True.

The screen was decided on the frontend from `state.statusData`, which is null
until `/api/status` succeeds. That call has an eight second timeout and is
made while the brain is still starting, which on this machine takes around
twenty. When it timed out the error was caught and logged, `statusData` stayed
null, and the check read `(null || {}).auth || {}` -- two undefined flags, both
falsy, so up came the welcome screen.

Every slow start therefore looked like a first run. The fix is a principle
rather than a longer timeout: absence of information is not evidence of a new
user, so the screen appears only when the backend has affirmatively said this
person has not been set up.

The logic is exercised in Node against the real app.js rather than asserted
about its source, because "the file contains this string" would not have
caught the original bug.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ONBOARDING = Path(__file__).resolve().parents[1] / "desk" / "ui" / "src" / "nova" / "onboarding.ts"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is needed to run the onboarding module"
)


def _decide(status) -> bool:
    """Run the real shouldShowOnboarding() from desk/ui under Node (which runs TypeScript)."""
    script = (f"const m = await import({json.dumps(ONBOARDING.as_uri())});"
              f"process.stdout.write(JSON.stringify(m.shouldShowOnboarding({json.dumps(status)})));")
    out = subprocess.run(["node", "--input-type=module", "-e", script],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_configured_user_is_never_asked_again():
    assert _decide({"auth": {"onboarded": True, "has_credential": True}}) is False


def test_a_user_with_a_key_but_no_flag_is_not_asked():
    assert _decide({"auth": {"onboarded": False, "has_credential": True}}) is False


def test_a_genuinely_new_user_is_asked():
    assert _decide({"auth": {"onboarded": False, "has_credential": False}}) is True


def test_an_unanswered_status_call_is_not_treated_as_a_new_user():
    """The actual bug: /api/status timed out and the screen came up."""
    assert _decide(None) is False, (
        "a status call that has not come back yet was read as 'never set up', "
        "so every slow start reopened the welcome screen"
    )


def test_a_status_response_without_auth_is_not_treated_as_a_new_user():
    assert _decide({}) is False
    assert _decide({"auth": None}) is False


def test_a_failed_status_call_is_not_treated_as_a_new_user():
    """setBackendState('error') leaves statusData untouched."""
    assert _decide({"auth": {}}) is False, (
        "an empty auth object meant 'undefined', which read as 'new user'"
    )


def test_the_decision_is_pure_and_takes_no_dom():
    """It has to be callable without a browser, or it cannot be tested."""
    body = ONBOARDING.read_text(encoding="utf-8")
    for forbidden in ("document.", "$(", "window."):
        assert forbidden not in body, (
            f"shouldShowOnboarding touches {forbidden}; keep the decision "
            f"separate from showing the thing"
        )


# ── the backend half ────────────────────────────────────────────────────────

def test_a_failure_to_read_the_credential_posture_asserts_nothing():
    """The other way this screen could reappear.

    `_auth_status()` used to answer an exception with onboarded=False and
    has_credential=False -- a positive claim that the user had never set NOVA
    up. The interface believed it. An error is the absence of evidence, not
    evidence of absence, so those keys are now simply not present.
    """
    import desk.bridge as bridge

    def explode():
        raise RuntimeError("credential store unavailable")

    original = None
    try:
        import desk.creds as creds
        original = creds.resolve
        creds.resolve = explode
        status = bridge._auth_status()
    finally:
        if original is not None:
            import desk.creds as creds
            creds.resolve = original

    assert status["mode"] == "unknown"
    assert "onboarded" not in status, (
        "a failed read claimed the user has never been set up"
    )
    assert "has_credential" not in status
    assert _decide({"auth": status}) is False, (
        "the interface would still reopen the welcome screen"
    )
