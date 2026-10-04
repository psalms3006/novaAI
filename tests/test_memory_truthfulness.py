"""Memory that does not contradict itself, and does not mistake a moment for a fact.

Both failures here were found in the user's real store, which held five
records. Three of them were wrong:

    "Project NOVA team's company name is OMNIEL"    confirmed, confidence 1.0
    "Project NOVA team's company name is Omnia."    confirmed, confidence 1.0
    "User opened setup screen, NOVA Cloud, own API key, or work offline
     prompt is visible"                             confirmed, confidence 1.0

Two confident answers to one question is worse than no answer, because
retrieval returns whichever is closest to the wording of the question and
nothing downstream can tell the store is in disagreement with itself. And the
third was true for about four seconds.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from living_memory import LivingMemory


def store():
    return LivingMemory(path=os.path.join(tempfile.mkdtemp(), "records.json"),
                        mirror=False)


def live(m):
    return [r for r in m.all_records() if not r.get("superseded_by")]


def texts(m):
    return [r["text"] for r in live(m)]


# ── one subject, one value ───────────────────────────────────────────────────

def test_restating_a_fact_differently_supersedes_it():
    """The exact failure in the real store. Neither sentence contains a
    correction word, because people do not announce corrections."""
    m = store()
    m.remember("Project NOVA team's company name is OMNIEL")
    m.remember("Project NOVA team's company name is Omnia.")
    assert texts(m) == ["Project NOVA team's company name is Omnia."], (
        "the store believes two different things about one question")


def test_the_superseded_version_is_kept_not_destroyed():
    """A correction that loses the original cannot itself be corrected."""
    m = store()
    m.remember("Project NOVA team's company name is Omnia")
    m.remember("Project NOVA team's company name is OMNIEL")
    old = [r for r in m.all_records() if r.get("superseded_by")]
    assert len(old) == 1
    assert "Omnia" in old[0]["text"]


def test_a_misspelled_name_can_be_corrected_by_saying_it_again():
    m = store()
    m.remember("User's full name is Samuel Chibuzor Asogwara")
    m.remember("User's full name is Samuel Chibuzor Asagwara")
    assert texts(m) == ["User's full name is Samuel Chibuzor Asagwara"]


def test_the_same_claim_twice_is_one_record():
    """"…is OMNIEL" and "…is OMNIEL." are one fact. Exact-text matching
    cannot see that; it is how a five-record store grows duplicates."""
    m = store()
    m.remember("Project NOVA team's company name is OMNIEL")
    m.remember("Project NOVA team's company name is OMNIEL.")
    assert len(live(m)) == 1


# ── and not more than that ───────────────────────────────────────────────────

def test_two_facts_about_one_noun_both_survive():
    """"NOVA is a voice assistant" and "NOVA is an OMNIEL product" are both
    true. A single shared noun is not a contradiction, and treating it as one
    would quietly delete half of what NOVA knows."""
    m = store()
    m.remember("NOVA is a voice assistant")
    m.remember("NOVA is an OMNIEL product")
    assert len(live(m)) == 2


def test_different_attributes_of_one_person_both_survive():
    m = store()
    m.remember("User's favourite colour is blue")
    m.remember("User's favourite food is rice")
    assert len(live(m)) == 2


def test_liking_two_things_is_not_a_contradiction():
    """Only the copula asserts a single value. "I like coffee" and "I like
    tea" are both true, so verbs must not be read as claims."""
    m = store()
    m.remember("User likes coffee")
    m.remember("User likes tea")
    assert len(live(m)) == 2


@pytest.mark.parametrize("vague", ["It is broken", "NOVA is slow", "This is fine"])
def test_a_subject_too_vague_to_match_on_supersedes_nothing(vague):
    """One noun and a value is not specific enough to overwrite anything."""
    m = store()
    m.remember("User's full name is Samuel Chibuzor Asagwara")
    m.remember(vague)
    assert len(live(m)) == 2


# ── a moment is not a fact ───────────────────────────────────────────────────

REAL = ("User opened setup screen, NOVA Cloud, own API key, or work offline "
        "prompt is visible")


def test_what_is_on_screen_is_not_remembered_as_a_fact():
    """Verbatim from the real store, where it sat permanently at confidence
    1.0 competing with the user's name."""
    m = store()
    with pytest.raises(ValueError):
        m.remember(REAL)
    assert live(m) == []


@pytest.mark.parametrize("passing", [
    "The settings dialog is open",
    "A Chrome tab is showing the docs",
    "The answer is on the screen right now",
    "User clicked the export button",
])
def test_passing_observations_are_refused(passing):
    m = store()
    with pytest.raises(ValueError):
        m.remember(passing)


def test_the_refusal_says_why():
    """The caller is usually the model deciding it has learned something. A
    reason is what stops it deciding that again."""
    m = store()
    with pytest.raises(ValueError, match="durable"):
        m.remember(REAL)


@pytest.mark.parametrize("durable", [
    "User's full name is Samuel Chibuzor Asagwara",
    "User prefers dark mode",
    "User studies Mechatronics at FUTO",
    "User's date of birth is June 30, 2006",
    "User is building NOVA",
])
def test_durable_facts_are_still_stored(durable):
    """The guard has to be narrow. Refusing real facts would be the worse
    failure of the two."""
    m = store()
    m.remember(durable)
    assert texts(m) == [durable]


def test_secrets_are_still_refused():
    """The guard this one was modelled on, unchanged."""
    m = store()
    with pytest.raises(ValueError, match="secret"):
        m.remember("User's api_key is abcd1234efgh5678")
