"""Turning "who is speaking" into "what may be done".

This does not implement permissions. `nova_core.permissions` already does,
properly — capabilities, trust levels, allow/confirm/deny, per-principal
grants and an audit log — and a second system answering the same question is
how two systems come to disagree about whether something is allowed.

What was missing is that its principals were agent roles: CODE, RESEARCH,
BROWSER, and "nova" for everything the user asked for directly. So every
request, whoever made it, arrived as the same principal with the same
authority. This registers one principal per authority level and answers the
one question the engine cannot: which principal is this request?

The mapping is deliberately conservative. A guest may hold a conversation and
open an application; they may not touch files, settings, credentials or
anybody's memory. Trusted users get the ordinary desktop verbs. Only the
owner reaches the things that would let someone change who NOVA answers to.
"""
from __future__ import annotations

from typing import Optional

from nova_core import permissions as perms
from nova_core.permissions import Capability as C, Effect, Grant, Trust

from .people import Authority, Identification

#: Principal names the permission engine knows these authorities by.
PRINCIPALS: dict[Authority, str] = {
    Authority.OWNER: "person:owner",
    Authority.TRUSTED: "person:trusted",
    Authority.KNOWN: "person:known",
    Authority.GUEST: "person:guest",
    Authority.UNKNOWN: "person:unknown",
}


def _caps(*names: C) -> frozenset[C]:
    return frozenset(names)


#: What each level of authority may do.
#:
#: An unknown speaker is not given nothing: refusing to talk to someone
#: because their voice is unfamiliar makes NOVA useless the first time
#: anybody new says hello. They get conversation and the ability to have
#: something looked up, and nothing that changes the machine or reveals
#: anything about the people who use it.
PERSON_GRANTS: dict[Authority, Grant] = {
    Authority.OWNER: Grant(
        PRINCIPALS[Authority.OWNER],
        _caps(C.FILE_READ, C.FILE_WRITE, C.FILE_DELETE, C.CODE_EXECUTE,
              C.PROCESS_CONTROL, C.APP_LAUNCH, C.NETWORK_READ,
              C.NETWORK_WRITE, C.BROWSER_READ, C.BROWSER_INTERACT,
              C.SYSTEM_SETTINGS, C.SYSTEM_READ, C.SYSTEM_ADJUST,
              C.CREDENTIAL_ACCESS, C.USER_DATA, C.MEMORY_READ, C.MEMORY_WRITE,
              C.SCREEN_READ, C.AUDIO_READ, C.MIC_CONTROL, C.CAMERA_ACCESS,
              C.SELF_MODIFY),
        # The owner confirms the same dangerous things NOVA always confirmed.
        # Highest authority is not the absence of a seatbelt, and a voice is
        # not a password: everything here is reachable by anyone who sounds
        # sufficiently like the owner, so the expensive verbs still ask.
        confirm=_caps(C.FILE_DELETE, C.SYSTEM_SETTINGS, C.PROCESS_CONTROL,
                      C.NETWORK_WRITE, C.CODE_EXECUTE, C.BROWSER_INTERACT,
                      C.CREDENTIAL_ACCESS, C.CAMERA_ACCESS, C.SELF_MODIFY),
        description="The person who owns this installation.",
    ),
    Authority.TRUSTED: Grant(
        PRINCIPALS[Authority.TRUSTED],
        _caps(C.FILE_READ, C.FILE_WRITE, C.APP_LAUNCH, C.NETWORK_READ,
              C.BROWSER_READ, C.BROWSER_INTERACT, C.SYSTEM_READ,
              C.SYSTEM_ADJUST, C.MEMORY_READ, C.MEMORY_WRITE, C.SCREEN_READ,
              C.AUDIO_READ),
        confirm=_caps(C.FILE_WRITE, C.BROWSER_INTERACT, C.MEMORY_WRITE),
        description="Someone the owner has vouched for.",
    ),
    Authority.KNOWN: Grant(
        PRINCIPALS[Authority.KNOWN],
        _caps(C.APP_LAUNCH, C.NETWORK_READ, C.BROWSER_READ, C.SYSTEM_READ,
              C.MEMORY_READ, C.FILE_READ, C.AUDIO_READ),
        confirm=_caps(C.FILE_READ, C.APP_LAUNCH),
        description="Enrolled, and nothing more than that.",
    ),
    Authority.GUEST: Grant(
        PRINCIPALS[Authority.GUEST],
        _caps(C.APP_LAUNCH, C.NETWORK_READ, C.BROWSER_READ, C.SYSTEM_READ,
              C.AUDIO_READ),
        confirm=_caps(C.APP_LAUNCH),
        description="Introduced themselves, which proves only that they can "
                    "talk.",
    ),
    Authority.UNKNOWN: Grant(
        PRINCIPALS[Authority.UNKNOWN],
        _caps(C.NETWORK_READ, C.AUDIO_READ),
        description="An unfamiliar voice. May be talked with; may not act.",
    ),
}


def install(engine: Optional[perms.PermissionEngine] = None) -> None:
    """Register the person principals with the permission engine.

    Idempotent, and additive: the agent-role grants the engine already has
    are untouched, because NOVA's own internal work still runs as those.
    """
    e = engine or perms.engine()
    for grant in PERSON_GRANTS.values():
        e.set_grant(grant)


def principal_for(identification: Identification) -> str:
    """Which principal a request from this person runs as.

    Uses `effective_authority`, not the person's nominal authority, so a
    merely probable owner does not act with an owner's hands.
    """
    return PRINCIPALS[identification.effective_authority]


def check_tool(identification: Identification, tool: str, *,
               args: Optional[dict] = None,
               trust: Trust = Trust.USER,
               engine: Optional[perms.PermissionEngine] = None):
    """May this person run this tool? Answered by the existing engine.

    Returns the engine's own Decision, so every caller keeps using one
    vocabulary of allow / confirm / deny and one audit log.
    """
    e = engine or perms.engine()
    install(e)
    return e.check_tool(principal_for(identification), tool,
                        trust=trust, args=args)


def outranks(a: Identification, b: Identification) -> bool:
    """Does a's instruction take precedence over b's?

    Strictly greater. Two people at the same level do not override each
    other -- the second one is simply the more recent thing said, which is
    ordinary conversation and not a conflict to resolve.
    """
    return a.effective_authority.rank > b.effective_authority.rank
