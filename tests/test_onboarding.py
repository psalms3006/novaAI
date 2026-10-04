"""First run should happen once.

The reported failure: paste a Gemini key, the screen closes, and seconds later
it is back asking again — over and over, until the user gives up and picks
"work offline", which finally sticks. Two things caused it, and the second one
made the first invisible.

And the escape hatch was itself dishonest: choosing offline left a
GEMINI_API_KEY from the environment in charge, so NOVA carried on calling
Gemini while the interface said offline.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk import creds

# The checks on the previous window (desk/static) that lived here moved to
# tests/test_desk_ui_behaviour.py when the interface became desk/ui.


@pytest.fixture
def clean_settings():
    """Restore whatever this machine was actually configured with."""
    saved = {k: creds._get_setting(k) for k in ("auth_mode", "onboarded")}
    yield
    creds.set_many({k: v for k, v in saved.items()})


def test_saving_a_key_counts_as_being_onboarded(clean_settings, monkeypatch):
    """The bug: it did not, so the first-run screen came straight back.

    Pasting a key *is* choosing how NOVA connects. The key was always stored
    correctly; only the flag that says a choice was made went unset, and the
    interface re-ran the same check a moment later and put the screen back up.
    """
    monkeypatch.setattr(creds, "store_byok", lambda k: None)
    monkeypatch.setattr(creds, "apply_runtime",
                        lambda: {"mode": "byok",
                                 "onboarded": bool(creds._get_setting("onboarded"))})
    creds.set_many({"onboarded": False})

    status = creds.set_byok("AQ." + "A" * 48)

    assert creds._get_setting("onboarded"), "the choice was not recorded"
    assert status.get("onboarded"), "the reply told the interface to ask again"


def test_every_way_of_finishing_marks_it_finished(clean_settings, monkeypatch):
    monkeypatch.setattr(creds, "apply_runtime", lambda: {"mode": "offline"})
    creds.set_many({"onboarded": False})
    creds.choose_offline()
    assert creds._get_setting("onboarded")


def test_choosing_offline_actually_goes_offline(clean_settings, monkeypatch):
    """An explicit choice outranks an ambient key.

    A GEMINI_API_KEY in the environment — from .env, or a developer shell —
    used to win unconditionally, so "work offline" left every request going to
    Gemini while the interface said offline. The one setting whose whole
    purpose is to stop network calls did not stop them.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "AQ." + "B" * 48)
    creds.set_many({"auth_mode": "offline", "onboarded": True})

    status = creds.resolve()

    assert status["mode"] == "offline"
    assert status["has_credential"] is False
    import os
    assert not os.environ.get("GEMINI_API_KEY"), (
        "the key is still in the environment; something will use it")


def test_an_environment_key_is_still_used_when_offline_was_not_chosen(
        clean_settings, monkeypatch):
    """The offline override must not break the ordinary path."""
    monkeypatch.setenv("GEMINI_API_KEY", "AQ." + "C" * 48)
    creds.set_many({"auth_mode": "env", "onboarded": True})

    status = creds.resolve()

    assert status["mode"] == "env"
    assert status["has_credential"] is True


