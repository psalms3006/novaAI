"""NOVA's willingness to push back is meant to be genuine, not just a
paragraph nobody reads. This pins that the directive actually exists in
the shared prompt (NOVA_CORE, which both NOVA_SYSTEM_PROMPT and
NOVA_OFFLINE_PROMPT are built from, so cloud and offline stay one NOVA
rather than two), and that it carries the two halves the user asked for
together: real directness, bounded by real respect.

A live-model A/B (the reliable way to confirm a prompt change actually
changes behaviour, not just prompt text) was attempted against the real
Gemini REST endpoint and could not be completed here -- the API returned
503 UNAVAILABLE twice in a row at the time this was written, a genuine
external outage, not a code or config problem. This is the fallback: a
content pin, not a behavioural proof.
"""
from __future__ import annotations

import nova


def test_the_directness_section_exists():
    assert "## Directness" in nova.NOVA_CORE


def test_it_permits_real_pushback():
    core = nova.NOVA_CORE.lower()
    assert "roast the decision" in core or "say so plainly" in core


def test_it_explicitly_bounds_the_pushback_with_respect():
    core = nova.NOVA_CORE.lower()
    assert "not the person" in core
    assert "not, ever" in core or "not unkind" in core or "not license to be unkind" in core


def test_cloud_and_offline_prompts_both_carry_it():
    """The same NOVA online and offline -- one identity, not two."""
    assert "## Directness" in nova.NOVA_SYSTEM_PROMPT
    assert "## Directness" in nova.NOVA_OFFLINE_PROMPT
