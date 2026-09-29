"""NOVA's willingness to push back lives in nova_personality (Layer A) and
reaches every model path: typed chat, voice, and the offline model. This pins
that it is there, that it carries both halves -- real directness, bounded by
respect and by the person's control -- and that no path goes without it.

Whether it actually changes behaviour is a different question, answered by
the live-model evaluation in tests/eval/personality_eval.py.
"""
from __future__ import annotations

import nova


def test_the_pushback_policy_exists():
    assert "## Agreeing and disagreeing" in nova.NOVA_SYSTEM_PROMPT


def test_it_permits_real_pushback():
    p = nova.NOVA_SYSTEM_PROMPT.lower()
    assert "push back when it matters" in p
    assert "you are not here to be agreed with" in p


def test_it_explicitly_bounds_the_pushback_with_respect_and_control():
    p = nova.NOVA_SYSTEM_PROMPT.lower()
    assert "never the person" in p
    assert "they decide" in p and "not refusal" in p


def test_every_model_path_carries_it():
    """One NOVA online, by voice, and offline -- not three."""
    assert "## Agreeing and disagreeing" in nova.NOVA_VOICE_PROMPT
    assert "disagree when the person is wrong" in nova.NOVA_OFFLINE_PROMPT.lower()


def test_the_offline_prompt_stays_small_enough_for_a_local_model():
    assert len(nova.NOVA_OFFLINE_PROMPT) < 3000
