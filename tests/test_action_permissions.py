"""A tool is not one risk level.

`computer_settings` covers both "what is the battery at" and "restart the
machine". Charging the whole tool at the rate of its most dangerous action
meant NOVA stopped to ask "should I proceed?" before *reading* a battery
percentage — and in a voice assistant that is not caution, it is an assistant
nobody can use, because every observation becomes a negotiation.

The other half matters more: making the common case quiet must not make the
dangerous case quiet too. These tests pin both directions.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nova_core import permissions as P
from nova_core import trust as T


def effect(tool, **args):
    return P.check_tool("nova", tool, args=args).effect.value


# ── the common case must be quiet ────────────────────────────────────────────

@pytest.mark.parametrize("action", [
    "battery", "get_volume", "get_brightness", "status", "info",
])
def test_reading_the_machine_needs_no_permission(action):
    assert effect("computer_settings", action=action) == "allow"


@pytest.mark.parametrize("action", [
    "volume_up", "volume_down", "volume_mute", "set_volume",
    "brightness_up", "brightness_down", "set_brightness",
])
def test_trivially_reversible_adjustments_are_allowed(action):
    """"Turn the volume up" does not benefit from "are you sure?"."""
    assert effect("computer_settings", action=action) == "allow"


@pytest.mark.parametrize("action", ["list", "read", "find", "search", "info"])
def test_looking_at_files_needs_no_permission(action):
    assert effect("file_controller", action=action) == "allow"


def test_launching_an_application_is_not_terminating_one():
    """The user just said "open Spotify". Asking them to confirm it is absurd."""
    assert effect("open_app", app_name="spotify") == "allow"
    assert effect("close_app", app_name="spotify") == "confirm"


def test_looking_at_the_screen_is_allowed():
    assert effect("computer_control", action="screenshot") == "allow"


# ── the dangerous case must stay loud ────────────────────────────────────────

@pytest.mark.parametrize("action", [
    "shutdown", "restart", "sleep", "hibernate", "logoff", "lock", "registry",
])
def test_actions_that_end_the_session_still_confirm(action):
    assert effect("computer_settings", action=action) == "confirm"


def test_deleting_a_file_confirms_as_a_deletion(tmp_path):
    """Not as a write.

    Falling back to the tool's declared set let file_controller with
    action="delete" through on FILE_WRITE, which is not the permission being
    exercised — and FILE_WRITE is not the one the policy stops to confirm. The
    first version of this fix shipped that hole; the test exists so it cannot
    come back.
    """
    assert effect("file_controller", action="delete", path=str(tmp_path)) == "confirm"
    caps = P.capabilities_for_tool("file_controller", {"action": "delete"})
    assert P.Capability.FILE_DELETE in caps


@pytest.mark.parametrize("action", ["kill", "terminate"])
def test_killing_a_process_confirms(action):
    assert effect("computer_control", action=action) == "confirm"


# ── none of this may weaken the trust boundary ───────────────────────────────

def test_untrusted_content_still_cannot_act():
    """A web page saying "open Spotify" is not the user saying it."""
    for tool, args in (("open_app", {"app_name": "x"}),
                       ("computer_settings", {"action": "volume_up"}),
                       ("file_controller", {"action": "delete", "path": "x"})):
        d = P.check_tool("nova", tool, args=args, trust=T.Trust.UNTRUSTED)
        assert d.effect.value in ("confirm", "deny"), (
            f"{tool}/{args} runs unchallenged on untrusted input")


def test_an_unknown_action_falls_back_to_the_strict_tool_policy():
    """Unrecognised means unclassified, which must not mean unrestricted."""
    assert effect("computer_settings", action="some_new_thing") == "confirm"


def test_a_tool_with_no_action_is_judged_as_before():
    assert P.check_tool("nova", "computer_settings").effect.value == "confirm"


def test_undeclared_tools_are_still_denied():
    assert P.check_tool("nova", "brand_new_undeclared_tool",
                        args={"action": "list"}).effect.value == "deny"
