"""A CONFIRM verdict has to reach someone who can say no.

`nova_core/permissions.py` describes itself as "the enforcement half of
prompt-injection defence", and it does the hard part correctly: at
`Trust.UNTRUSTED` it returns CONFIRM for `computer_control`, `open_app`,
`close_app`, `app_control` and `computer_settings` — the tools that actually
operate the machine.

That verdict was then discarded twice over. `nova_safety.safety_gate` returns
None immediately for any tool not in its seven-name `CONSEQUENTIAL_TOOLS`
map, and none of those five are in it. And when it did consult the engine, it
called `check_tool` without `trust=`, so the engine answered for `Trust.USER`
— an ALLOW that silently overrode the CONFIRM the caller had obtained under
taint.

Synthetic keystrokes are indistinguishable from the user typing, so this is
the highest-leverage ungated action in the codebase: a web page that
`web_search` just read could ask for them and nobody would be consulted.

What these tests pin is deliberately narrow. An ordinary turn must not start
asking permission to move the mouse — that would make voice unusable, and the
team already fought that battle once over battery readings. The question is
only asked when the turn has been touched by content NOVA did not get from
the user.
"""
from __future__ import annotations

import pytest

from nova_core import trust as _trust
from nova_core.permissions import Trust
import nova_safety


TYPING = ("computer_control", {"action": "type", "text": "hello"})
CLICKING = ("computer_control", {"action": "click", "x": 10, "y": 10})
LAUNCHING = ("open_app", {"app_name": "notepad"})


@pytest.fixture
def declines():
    """A user who says no, and a record of whether they were asked at all."""
    record = {"asked": 0}

    def get_confirmation():
        record["asked"] += 1
        return "no"

    record["fn"] = get_confirmation
    return record


@pytest.mark.parametrize("tool,args", [TYPING, CLICKING, LAUNCHING])
def test_an_ordinary_turn_is_not_interrupted(declines, tool, args):
    """The user asked for this themselves. Do not stop and check."""
    verdict = nova_safety.safety_gate(
        tool, args, declines["fn"], trust=Trust.USER,
    )
    assert verdict is None, f"NOVA asked before {tool} in an ordinary turn"
    assert declines["asked"] == 0


@pytest.mark.parametrize("tool,args", [TYPING, CLICKING, LAUNCHING])
def test_a_tainted_turn_has_to_ask_first(declines, tool, args):
    """The instruction came from a web page. Someone must agree to it."""
    verdict = nova_safety.safety_gate(
        tool, args, declines["fn"], trust=Trust.UNTRUSTED,
    )
    assert declines["asked"] >= 1, (
        f"{tool} ran under taint without anyone being asked"
    )
    assert verdict is not None, f"{tool} proceeded after the user declined"


def test_the_gate_reads_the_turn_s_trust_when_it_is_not_told(declines):
    """Callers should not have to remember to pass it.

    `_execute_tool_sync` calls the gate without a trust argument, so the gate
    has to look the context up itself or the fix reaches nothing real.
    """
    with _trust.untrusted("a web page"):
        verdict = nova_safety.safety_gate(*TYPING, declines["fn"])

    assert declines["asked"] >= 1, "the gate ignored the ambient taint"
    assert verdict is not None


def test_saying_yes_lets_a_tainted_action_proceed():
    """Guard against fixing this by refusing everything under taint."""
    asked = {"n": 0}

    def approves():
        asked["n"] += 1
        return "yes"

    verdict = nova_safety.safety_gate(
        *TYPING, approves, trust=Trust.UNTRUSTED,
    )
    assert asked["n"] >= 1
    assert verdict is None, "an approved action was blocked anyway"


def test_the_seven_consequential_tools_still_ask_in_an_ordinary_turn(declines):
    """Existing behaviour must not regress: deleting files still asks."""
    verdict = nova_safety.safety_gate(
        "file_controller", {"action": "delete", "path": "x.txt"},
        declines["fn"], trust=Trust.USER,
    )
    assert declines["asked"] >= 1
    assert verdict is not None


# ── a declined confirmation must not become a retry loop ───────────────────
#
# From a real session (2026-09-23): a computer_settings confirmation that got
# no answer was retried by the model every ~20.5s for three-plus minutes,
# dropping the user's mic audio the whole time because the session never got
# back to processing anything else. "I won't do X without your yes" reads as
# a status, not an instruction not to try again -- the model kept trying.

def test_a_timed_out_confirmation_tells_the_model_not_to_retry():
    def no_answer():
        return ""  # VoiceConfirmer.ask() returns "" when nothing was heard

    verdict = nova_safety.safety_gate(
        "file_controller", {"action": "delete", "path": "x.txt"},
        no_answer, trust=Trust.USER,
    )
    assert verdict is not None
    assert "do not" in verdict.lower() or "don't" in verdict.lower(), (
        "a timed-out confirmation did not tell the model to stop retrying"
    )


def test_an_explicit_decline_also_tells_the_model_not_to_retry(declines):
    verdict = nova_safety.safety_gate(
        "file_controller", {"action": "delete", "path": "x.txt"},
        declines["fn"], trust=Trust.USER,
    )
    assert verdict is not None
    assert "do not" in verdict.lower() or "don't" in verdict.lower()
