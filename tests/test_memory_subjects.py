"""Memory that knows whose memory it is.

Every record used to be about "the user", because there was only ever one.
Once NOVA can tell two people apart that assumption becomes a bug with two
halves: Chizi's preferences get filed under Samuel, and "what do you know
about Samuel" becomes a way to read the owner's memory out of the machine by
standing next to it and asking.

So a record has a subject — who it is about — and an author — who said it.
They are usually the same person and occasionally are not, and that gap is
the reason both exist: "Chizi prefers tea", said by Samuel, is a fact about
Chizi on Samuel's authority.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from living_memory import DEFAULT_SUBJECT, LivingMemory


def store():
    return LivingMemory(path=os.path.join(tempfile.mkdtemp(), "records.json"),
                        mirror=False)


def live(m):
    return [r for r in m.all_records() if not r.get("superseded_by")]


# ── keeping people apart ─────────────────────────────────────────────────────

def test_two_people_may_disagree(store_=None):
    """The same attribute with different values is a contradiction within one
    person and ordinary life between two."""
    m = store()
    m.remember("favourite colour is blue", subject_id="samuel")
    m.remember("favourite colour is green", subject_id="chizi")
    assert len(live(m)) == 2, "one person's answer overwrote another's"


def test_one_person_still_contradicts_themselves():
    m = store()
    m.remember("favourite colour is blue", subject_id="samuel")
    m.remember("favourite colour is green", subject_id="samuel")
    assert len(live(m)) == 1
    assert "green" in live(m)[0]["text"]


def test_a_record_knows_who_it_is_about():
    m = store()
    rec = m.remember("prefers tea", subject_id="chizi", author_id="samuel")
    assert rec["subject_id"] == "chizi"
    assert rec["author_id"] == "samuel", (
        "who said it was lost; the fact now looks self-reported")


def test_the_author_defaults_to_the_subject():
    m = store()
    rec = m.remember("prefers tea", subject_id="chizi")
    assert rec["author_id"] == "chizi"


def test_the_same_fact_about_two_people_is_two_records():
    m = store()
    m.remember("studies Mechatronics", subject_id="samuel")
    m.remember("studies Mechatronics", subject_id="chizi")
    assert len(live(m)) == 2


# ── what was there before ────────────────────────────────────────────────────

def test_records_written_before_subjects_existed_belong_to_the_owner():
    """They were all written on a single-user machine about its owner, so
    attributing them to the owner is not a guess. Leaving them unattributed
    would hide them from the one person they describe."""
    m = store()
    legacy = {"id": "mem_old", "text": "User's name is Samuel",
              "type": "fact", "importance": 0.9, "confidence": 1.0,
              "source": "explicit", "project": "", "confirmed": True,
              "created": 0.0, "updated": 0.0, "last_accessed": 0.0,
              "access_count": 0, "decay": {"active": False, "expires": None},
              "superseded_by": None, "versions": [], "meta": {}}
    m._records.append(legacy)
    assert m.subject_of(legacy) == DEFAULT_SUBJECT

    found = m.search("Samuel name", reader_id=DEFAULT_SUBJECT,
                     can_read_others=False)
    assert any(r["id"] == "mem_old" for r in found), (
        "a record from before subjects existed became invisible")


def test_a_legacy_record_can_still_be_corrected():
    m = store()
    m._records.append({
        "id": "mem_old", "text": "favourite colour is blue", "type": "fact",
        "importance": 0.9, "confidence": 1.0, "source": "explicit",
        "project": "", "confirmed": True, "created": 0.0, "updated": 0.0,
        "last_accessed": 0.0, "access_count": 0,
        "decay": {"active": False, "expires": None},
        "superseded_by": None, "versions": [], "meta": {}})
    m.remember("favourite colour is red", subject_id=DEFAULT_SUBJECT)
    assert len(live(m)) == 1 and "red" in live(m)[0]["text"]


# ── privacy ──────────────────────────────────────────────────────────────────

def test_a_guest_cannot_read_the_owners_memory():
    """"What do you know about Samuel", asked by somebody else."""
    m = store()
    m.remember("bank is First Bank", subject_id="samuel")
    m.remember("favourite colour is green", subject_id="chizi")

    got = m.search("Samuel bank", reader_id="chizi", can_read_others=False)
    assert all(m.subject_of(r) == "chizi" for r in got), (
        "the owner's memory was read by someone else: "
        + repr([r["text"] for r in got]))


def test_a_guest_can_still_read_what_is_known_about_them():
    """A guest with no memory at all is a guest NOVA cannot hold a
    conversation with."""
    m = store()
    m.remember("prefers tea", subject_id="chizi")
    got = m.search("tea", reader_id="chizi", can_read_others=False)
    assert got and got[0]["text"] == "prefers tea"


def test_nova_working_for_the_owner_sees_everything():
    """The default has to stay unrestricted. Every existing caller is NOVA
    acting for the owner, and narrowing those silently would be a loss of
    memory dressed up as a security improvement."""
    m = store()
    m.remember("bank is First Bank", subject_id="samuel")
    m.remember("prefers tea", subject_id="chizi")
    assert len(m.search("")) >= 2


def test_asking_on_someones_behalf_without_restriction_is_explicit():
    """can_read_others defaults to True, so a caller that wants the
    restriction has to say so — a caller that forgets gets the old behaviour
    rather than a silently emptied memory."""
    m = store()
    m.remember("bank is First Bank", subject_id="samuel")
    assert m.search("bank", reader_id="chizi")


# ── the guards still apply per person ────────────────────────────────────────

def test_secrets_are_refused_whoever_they_are_about():
    m = store()
    with pytest.raises(ValueError, match="secret"):
        m.remember("api_key is abcd1234efgh5678", subject_id="chizi")


def test_passing_observations_are_refused_whoever_they_are_about():
    m = store()
    with pytest.raises(ValueError, match="durable"):
        m.remember("The settings dialog is open", subject_id="chizi")
