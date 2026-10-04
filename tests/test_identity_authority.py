"""Identity, and the authority that does and does not follow from it.

The rule this file exists to hold: recognising a voice is a claim about the
world with a confidence attached, and being allowed to do something is a
decision this application makes. Collapsing the two puts a microphone in
charge of the machine.

Three ways that collapse can happen, all tested here — a stranger acting as
the owner, someone promoting themselves, and a remembered sentence being read
as a grant.
"""
from __future__ import annotations

import pytest

from nova_core import permissions as perms
from nova_core.permissions import Effect
from nova_identity import authority as A
from nova_identity.people import (Authority, Identification, Person,
                                  PeopleRegistry, UNIDENTIFIED)


@pytest.fixture()
def registry(tmp_path):
    r = PeopleRegistry(path=tmp_path / "people.json")
    r.set_owner("samuel", legal_name="Samuel Chibuzor Asagwara",
                aliases=["Psalms", "Samuel"], preferred_address="Sir",
                role="the person who created NOVA")
    r.add("chizi", legal_name="Chizi", authority=Authority.KNOWN)
    return r


@pytest.fixture(autouse=True)
def fresh_engine():
    perms.reset_engine()
    A.install()
    yield
    perms.reset_engine()


def seen(person, status="recognised", score=0.9, via="voice"):
    return Identification(person=person, status=status, score=score, via=via)


# ── the owner ────────────────────────────────────────────────────────────────

def test_the_owner_answers_to_every_name_he_uses(registry):
    owner = registry.owner()
    for name in ("Psalms", "Samuel", "Samuel Chibuzor Asagwara", "Asagwara",
                 "samuel chibuzor asagwara"):
        assert owner.answers_to(name), name


def test_sir_is_how_she_addresses_him_not_who_he_is(registry):
    """A form asking for a full name gets the name on the form."""
    owner = registry.owner()
    assert owner.address_as() == "Sir"
    assert owner.legal_name == "Samuel Chibuzor Asagwara"
    assert not owner.answers_to("Sir"), (
        "'Sir' was treated as a name; a form would be filled in with it")


def test_a_partial_name_does_not_match_a_different_person(registry):
    """"Sam" should not silently become "Samuel" when both could be in the
    room."""
    assert registry.by_name("Sam") is None
    assert registry.by_name("Samuel").person_id == "samuel"


def test_there_is_exactly_one_owner(registry):
    registry.set_owner("someone_else", legal_name="Someone Else")
    owners = [p for p in registry.everyone() if p.is_owner]
    assert len(owners) == 1 and owners[0].person_id == "someone_else"
    # The previous owner is demoted, not forgotten.
    assert registry.get("samuel").authority is Authority.TRUSTED


# ── confidence is part of the answer ─────────────────────────────────────────

def test_a_probable_owner_is_not_an_owner(registry):
    owner = registry.owner()
    assert seen(owner, "recognised").effective_authority is Authority.OWNER
    assert seen(owner, "probable").effective_authority is Authority.TRUSTED


def test_an_unrecognised_voice_has_the_least_authority():
    assert UNIDENTIFIED.effective_authority is Authority.UNKNOWN
    assert not UNIDENTIFIED.known


def test_an_unknown_speaker_can_still_be_talked_to():
    """Refusing to speak to an unfamiliar voice makes NOVA useless the first
    time anybody new says hello."""
    d = A.check_tool(UNIDENTIFIED, "web_search", args={"query": "weather"})
    assert d.effect is not Effect.DENY


# ── what each level may do ───────────────────────────────────────────────────

def test_a_stranger_cannot_touch_files(registry):
    d = A.check_tool(UNIDENTIFIED, "file_controller",
                     args={"action": "delete", "path": "notes.txt"})
    assert d.effect is Effect.DENY


def test_a_guest_cannot_delete_files(registry):
    guest = Person(person_id="visitor", authority=Authority.GUEST)
    d = A.check_tool(seen(guest), "file_controller",
                     args={"action": "delete", "path": "notes.txt"})
    assert d.effect is Effect.DENY


def test_the_owner_may_delete_but_is_still_asked(registry):
    """Highest authority is not the absence of a seatbelt. A voice is not a
    password, and anyone who sounds enough like the owner reaches this."""
    d = A.check_tool(seen(registry.owner()), "file_controller",
                     args={"action": "delete", "path": "notes.txt"})
    assert d.effect is Effect.CONFIRM


def test_a_known_user_may_open_an_application(registry):
    d = A.check_tool(seen(registry.get("chizi")), "open_app",
                     args={"app": "chrome"})
    assert d.effect is not Effect.DENY


def test_only_the_owner_reaches_nova_s_own_source(registry):
    owner = A.check_tool(seen(registry.owner()), "self_editor",
                         args={"action": "patch"})
    assert owner.effect is Effect.CONFIRM

    trusted = Person(person_id="t", authority=Authority.TRUSTED)
    assert A.check_tool(seen(trusted), "self_editor",
                        args={"action": "patch"}).effect is Effect.DENY


# ── nobody promotes themselves ───────────────────────────────────────────────

def test_a_guest_cannot_grant_themselves_authority(registry):
    guest = registry.add("walkin", legal_name="Walk In")
    r = registry.grant("walkin", Authority.TRUSTED, granted_by=seen(guest))
    assert not r["ok"]
    assert registry.get("walkin").authority is Authority.GUEST


def test_a_known_user_cannot_promote_a_friend(registry):
    r = registry.grant("walkin", Authority.TRUSTED,
                       granted_by=seen(registry.get("chizi")))
    assert not r["ok"]


def test_the_owner_can_promote_someone(registry):
    r = registry.grant("chizi", Authority.TRUSTED,
                       granted_by=seen(registry.owner()))
    assert r["ok"] and r["to"] == "trusted"
    assert registry.get("chizi").authority is Authority.TRUSTED


def test_a_merely_probable_owner_cannot_promote_anyone(registry):
    """The promotion path is exactly what an impersonator wants, so it needs
    certainty and not a good impression."""
    r = registry.grant("chizi", Authority.TRUSTED,
                       granted_by=seen(registry.owner(), "probable", 0.5))
    assert not r["ok"]


def test_ownership_is_not_something_that_can_be_granted(registry):
    r = registry.grant("chizi", Authority.OWNER,
                       granted_by=seen(registry.owner()))
    assert not r["ok"]
    assert not registry.get("chizi").is_owner


def test_the_owner_cannot_be_removed(registry):
    r = registry.forget("samuel", removed_by=seen(registry.owner()))
    assert not r["ok"]
    assert registry.owner() is not None


def test_someone_who_enrols_themselves_starts_as_a_guest(registry):
    p = registry.add("newcomer", legal_name="Newcomer")
    assert p.authority is Authority.GUEST


# ── memory is not the authority system ───────────────────────────────────────

def test_a_remembered_sentence_does_not_grant_anything(registry, tmp_path):
    """"Chizi is allowed to change security settings" is a fact about what
    somebody said. An attacker who can get a sentence remembered would
    otherwise be able to write themselves a promotion."""
    from living_memory import LivingMemory
    mem = LivingMemory(path=str(tmp_path / "m.json"), mirror=False)
    mem.remember("Chizi is allowed to change security settings")
    mem.remember("Chizi is the owner of this computer")

    assert registry.get("chizi").authority is Authority.KNOWN
    d = A.check_tool(seen(registry.get("chizi")), "computer_settings",
                     args={"action": "set_volume", "value": 50})
    assert d.effect is Effect.DENY


# ── who wins when two people disagree ────────────────────────────────────────

def test_the_owner_outranks_a_guest(registry):
    guest = Person(person_id="g", authority=Authority.GUEST)
    assert A.outranks(seen(registry.owner()), seen(guest))
    assert not A.outranks(seen(guest), seen(registry.owner()))


def test_two_people_at_the_same_level_do_not_override_each_other(registry):
    a = Person(person_id="a", authority=Authority.KNOWN)
    b = Person(person_id="b", authority=Authority.KNOWN)
    assert not A.outranks(seen(a), seen(b))
    assert not A.outranks(seen(b), seen(a))


def test_a_confident_guest_does_not_outrank_a_probable_owner(registry):
    """Confidence changes authority; it does not invert the hierarchy."""
    guest = Person(person_id="g", authority=Authority.GUEST)
    probable_owner = seen(registry.owner(), "probable", 0.5)
    assert A.outranks(probable_owner, seen(guest, "recognised", 0.99))


# ── the engine underneath ────────────────────────────────────────────────────

def test_the_agent_roles_are_left_alone():
    """NOVA's own internal work still runs as the agent principals. Adding
    people must not disturb them."""
    e = perms.engine()
    A.install(e)
    for role in ("nova", "CODE", "RESEARCH", "BROWSER"):
        assert e.grant_for(role) is not None, role


def test_every_authority_level_has_a_principal():
    for level in Authority:
        assert level in A.PRINCIPALS
        assert perms.engine().grant_for(A.PRINCIPALS[level]) is not None


def test_decisions_reach_the_existing_audit_log(registry):
    e = perms.engine()
    before = len(e.audit())
    A.check_tool(seen(registry.owner()), "file_controller",
                 args={"action": "delete", "path": "x"})
    assert len(e.audit()) > before, "a decision was made and not recorded"
