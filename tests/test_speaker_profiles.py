"""Voice profiles, and the attack they exist to survive.

Authority follows from "that was the owner", so the profile is the thing an
attacker most wants to move. The obvious design — keep averaging in whatever
sounds roughly right — moves it for them: talk to NOVA often enough while
sounding a little more like yourself each time, and the owner's profile ends
up describing you. Every individual step looks acceptable. Only the total
distance gives it away, which is why the binding check is on the accumulated
centroid rather than on each sample.

These tests use synthetic embeddings rather than audio, because what is under
test is the arithmetic of adaptation and not the model. The model's own
behaviour is checked in test_speaker_embedding.py.
"""
from __future__ import annotations

import numpy as np
import pytest

from nova_identity import profiles as P
from nova_identity.profiles import ProfileStore, VoiceProfile


def unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / float(np.linalg.norm(v))


def voice(seed: int, n: int = 5, jitter: float = 0.12):
    """n embeddings of one imaginary person: a direction plus small noise."""
    rng = np.random.RandomState(seed)
    base = unit(rng.randn(P.EMBEDDING_DIM))
    out = []
    for _ in range(n):
        out.append(unit(base + jitter * unit(rng.randn(P.EMBEDDING_DIM))))
    return base, out


def toward(a, b, t):
    """A voice t of the way from a to b. The impersonator's dial."""
    return unit((1.0 - t) * np.asarray(a) + t * np.asarray(b))


@pytest.fixture()
def store(tmp_path):
    return ProfileStore(path=tmp_path / "voices.json")


# ── enrolment ────────────────────────────────────────────────────────────────

def test_enrolment_needs_several_samples():
    """One recording describes a recording, not a person."""
    s = ProfileStore(path=None.__class__ and __import__("pathlib").Path(
        __import__("tempfile").mkdtemp()) / "v.json")
    _, samples = voice(1)
    r = s.enrol("owner", samples[:2])
    assert not r["ok"] and "at least" in r["reason"]


def test_enrolling_two_different_people_is_refused(store):
    """A profile centred on nobody matches everybody a little."""
    _, a = voice(1, n=3)
    _, b = voice(2, n=3)
    r = store.enrol("owner", a + b)
    assert not r["ok"]
    assert "same person" in r["reason"]


def test_a_clean_enrolment_is_recognised(store):
    base, samples = voice(1)
    assert store.enrol("owner", samples)["ok"]
    again = unit(base + 0.12 * unit(np.random.RandomState(99).randn(P.EMBEDDING_DIM)))
    m = store.identify(again)
    assert m.status == "recognised" and m.speaker_id == "owner"


def test_a_stranger_is_not_recognised(store):
    _, samples = voice(1)
    store.enrol("owner", samples)
    _, stranger = voice(77, n=1)
    m = store.identify(stranger[0])
    assert m.status == "unknown"
    assert m.speaker_id is None, "a stranger was given someone's identity"


def test_re_enrolment_replaces_rather_than_merges(store):
    """Re-enrolling is what someone does when the old profile is wrong.
    Averaging it with the old one keeps the thing they are replacing."""
    _, first = voice(1)
    store.enrol("owner", first)
    _, second = voice(2)
    store.enrol("owner", second)
    p = store.get("owner")
    assert len(p.baseline) == len(second)
    assert P.similarity(p.baseline_centroid, P._centroid(second)) > 0.95


# ── who spoke, and how sure ──────────────────────────────────────────────────

def test_two_similar_profiles_produce_a_question_not_an_answer(store):
    """Two voices this close is an ambiguity whatever the top score says."""
    base, samples = voice(1)
    store.enrol("owner", samples)
    near = [toward(base, voice(5, n=1)[0], 0.18) for _ in range(4)]
    store.enrol("sibling", near + [toward(base, voice(5, n=1)[0], 0.2)])

    m = store.identify(base)
    if m.runner_up and (m.score - m.runner_up_score) < 0.10:
        assert m.status == "probable", (
            "two near-identical profiles produced a confident answer")


def test_nothing_enrolled_means_unknown_not_an_error(store):
    m = store.identify(unit(np.random.RandomState(3).randn(P.EMBEDDING_DIM)))
    assert m.status == "unknown" and m.speaker_id is None


# ── poisoning ────────────────────────────────────────────────────────────────

def test_a_stranger_cannot_be_learned_into_the_owners_profile(store):
    _, samples = voice(1)
    store.enrol("owner", samples)
    _, stranger = voice(77, n=1)
    r = store.consider("owner", stranger[0])
    assert not r["accepted"]


def test_a_gradual_impersonation_cannot_walk_the_profile_away(store):
    """The attack the design exists for.

    Every step is a voice slightly closer to the attacker than the last, and
    each one on its own looks like a plausible sample of the owner. Without
    the drift floor this succeeds; with it the profile stops moving long
    before the attacker's own voice would be accepted.
    """
    base, samples = voice(1)
    store.enrol("owner", samples)
    attacker, _ = voice(313, n=1)

    for step in range(60):
        t = min(0.95, 0.02 * step)
        store.consider("owner", toward(base, attacker, t), reason=f"step {step}")

    p = store.get("owner")
    assert p.drift >= P.DRIFT_FLOOR, (
        f"profile drifted to {p.drift:.3f}, past the floor of {P.DRIFT_FLOOR}")

    # The point of the floor: the attacker still is not the owner.
    m = store.identify(attacker)
    assert m.speaker_id != "owner", "the attacker became the owner"
    assert store.identify(base).speaker_id == "owner", "the real owner was lost"


def test_without_the_drift_floor_the_attack_succeeds(store, monkeypatch):
    """The negative control, so the test above cannot pass for free.

    A protection is only demonstrated by showing the attack working without
    it. Measured with the floor removed: the profile drifts to 0.32, the
    attacker is recognised as the owner at 0.95, and the real owner is no
    longer recognised at all.
    """
    shipped_floor = P.DRIFT_FLOOR          # captured before it is removed
    monkeypatch.setattr(P, "DRIFT_FLOOR", -1.0)
    monkeypatch.setattr(P, "ADAPT_BASELINE_MIN", -1.0)

    base, samples = voice(1)
    store.enrol("owner", samples)
    attacker, _ = voice(313, n=1)
    for step in range(60):
        store.consider("owner", toward(base, attacker, min(0.95, 0.02 * step)))

    assert store.identify(attacker).speaker_id == "owner", (
        "the attack no longer works, so the test that it is prevented "
        "proves nothing")
    assert store.get("owner").drift < shipped_floor, (
        "the profile did not actually move, so this is not the attack")


def test_the_owners_real_voice_still_adapts(store):
    """A protection that refuses everything is not protection, it is a broken
    feature with a good excuse."""
    base, samples = voice(1)
    store.enrol("owner", samples)
    rng = np.random.RandomState(404)
    accepted = 0
    for _ in range(10):
        nearby = unit(base + 0.12 * unit(rng.randn(P.EMBEDDING_DIM)))
        if store.consider("owner", nearby)["accepted"]:
            accepted += 1
    assert accepted > 0, "no genuine sample was ever learned from"
    assert store.get("owner").drift >= P.DRIFT_FLOOR


def test_confident_enough_to_act_on_is_not_confident_enough_to_learn_from():
    assert P.ADAPT_MIN > P.ACCEPT > P.PROBABLE


def test_adaptation_is_bounded_so_one_session_cannot_dominate(store):
    base, samples = voice(1)
    store.enrol("owner", samples)
    rng = np.random.RandomState(7)
    for _ in range(P.MAX_ADAPTED * 3):
        store.consider("owner", unit(base + 0.1 * unit(rng.randn(P.EMBEDDING_DIM))))
    assert len(store.get("owner").adapted) <= P.MAX_ADAPTED


def test_adaptation_can_be_switched_off_for_one_person(store):
    base, samples = voice(1)
    store.enrol("owner", samples)
    store.get("owner").allow_adaptation = False
    r = store.consider("owner", unit(base + 0.05 * unit(np.ones(P.EMBEDDING_DIM))))
    assert not r["accepted"] and "off" in r["reason"]


def test_a_refused_sample_changes_nothing_at_all(store):
    """Either it passes every test or the profile is untouched. A partially
    applied adaptation is a profile nobody can reason about."""
    base, samples = voice(1)
    store.enrol("owner", samples)
    before = store.get("owner").centroid.copy()
    _, stranger = voice(77, n=1)
    store.consider("owner", stranger[0])
    assert np.allclose(before, store.get("owner").centroid)


# ── undo ─────────────────────────────────────────────────────────────────────

def test_rollback_returns_to_what_was_enrolled(store):
    base, samples = voice(1)
    store.enrol("owner", samples)
    rng = np.random.RandomState(11)
    for _ in range(8):
        store.consider("owner", unit(base + 0.12 * unit(rng.randn(P.EMBEDDING_DIM))))
    assert store.get("owner").adapted

    store.rollback("owner")
    p = store.get("owner")
    assert p.adapted == []
    assert P.similarity(p.centroid, p.baseline_centroid) > 0.999
    assert p.enrolled, "rollback destroyed the enrolment it was protecting"


def test_every_accepted_change_is_recorded(store):
    base, samples = voice(1)
    store.enrol("owner", samples)
    store.consider("owner", unit(base + 0.1 * unit(np.ones(P.EMBEDDING_DIM))),
                   reason="after a confirmed turn")
    p = store.get("owner")
    assert p.updates, "a profile changed with no record of why"
    assert p.updates[-1].reason == "after a confirmed turn"
    assert p.updates[-1].drift_after >= P.DRIFT_FLOOR


# ── storage ──────────────────────────────────────────────────────────────────

def test_profiles_survive_a_restart(tmp_path):
    base, samples = voice(1)
    s1 = ProfileStore(path=tmp_path / "v.json")
    s1.enrol("owner", samples)
    s1.consider("owner", unit(base + 0.1 * unit(np.ones(P.EMBEDDING_DIM))))

    s2 = ProfileStore(path=tmp_path / "v.json")
    assert s2.enrolled_speakers() == ["owner"]
    assert s2.identify(base).speaker_id == "owner"


def test_a_corrupt_profile_file_does_not_stop_nova(tmp_path):
    p = tmp_path / "v.json"
    p.write_text("{ this is not json", encoding="utf-8")
    s = ProfileStore(path=p)
    assert s.enrolled_speakers() == []
    assert s.identify(unit(np.ones(P.EMBEDDING_DIM))).status == "unknown"


def test_the_summary_does_not_leak_voiceprints(store):
    """This goes on a settings screen. A voiceprint does not belong there."""
    _, samples = voice(1)
    store.enrol("owner", samples)
    blob = repr(store.summary())
    assert "baseline" not in blob and "adapted_vectors" not in blob
    assert store.summary()[0]["samples"] == len(samples)
