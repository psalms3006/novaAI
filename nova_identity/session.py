"""Who is speaking to NOVA at this moment, for the rest of the process to ask.

One question, one answer, in one place. The alternative is each subsystem
forming its own opinion from whatever it can see, and those opinions
disagreeing at exactly the moment it matters.

The default is that nobody is identified. That is the honest starting state
and it is also the safe one: `UNIDENTIFIED` carries the lowest authority, so
a subsystem that forgets to ask still cannot be tricked into acting as the
owner.

There is one deliberate exception, and it is written down rather than
implied. On an installation where nobody has enrolled a voice, the person at
the keyboard is treated as the owner — because that is exactly what NOVA did
before any of this existed, and quietly demoting a working single-user
machine to "unknown speaker" would break it in the name of security it is not
getting. The moment a voice is enrolled, recognition governs and the
assumption stops.
"""
from __future__ import annotations

import threading
from typing import Optional

from .people import Authority, Identification, PeopleRegistry, UNIDENTIFIED

_lock = threading.RLock()
_current: Identification = UNIDENTIFIED


def current_speaker() -> Identification:
    """Who NOVA believes she is talking to. Never None."""
    with _lock:
        return _current


def set_speaker(identification: Identification) -> Identification:
    with _lock:
        global _current
        _current = identification or UNIDENTIFIED
        return _current


def clear_speaker() -> None:
    set_speaker(UNIDENTIFIED)


def anyone_enrolled() -> bool:
    """Has a voice been enrolled on this machine?

    The switch between "single user at the keyboard" and "recognition
    governs". Failure is read as yes-enrolled, because the safe answer to
    "can I verify who this is" going wrong is to stop assuming.
    """
    try:
        from .profiles import ProfileStore
        return bool(ProfileStore().enrolled_speakers())
    except Exception:
        return True


def assume_local_owner(registry: Optional[PeopleRegistry] = None) -> Identification:
    """Treat the person at this machine as the owner, where that is honest.

    Called by the entry points where a human is physically present. Does
    nothing once any voice is enrolled: from then on the answer comes from
    the microphone and not from an assumption, and silently overriding that
    would make enrolment worse than useless.

    `via` records how the belief was formed, so an audit entry never implies
    a voice was recognised when nobody was listening.
    """
    reg = registry or PeopleRegistry()
    owner = reg.owner()
    if owner is None:
        return current_speaker()
    if anyone_enrolled():
        return current_speaker()
    return set_speaker(Identification(
        person=owner, status="recognised", score=1.0, via="local session"))


def identify_by_name(name: str,
                     registry: Optional[PeopleRegistry] = None) -> Identification:
    """Someone said who they are. Believe it, but record that it was a claim.

    `via="claimed"` and a merely probable status, because a name is not
    evidence: it is the easiest thing in the world to say. It is enough to
    hold a conversation with somebody and to call them what they asked to be
    called, and `effective_authority` drops it a level so it is not enough to
    act on.
    """
    reg = registry or PeopleRegistry()
    person = reg.by_name(name)
    if person is None:
        return set_speaker(UNIDENTIFIED)
    return set_speaker(Identification(person=person, status="probable",
                                      score=0.0, via="claimed"))
