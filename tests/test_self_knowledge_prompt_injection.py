"""The self-knowledge summary must actually reach the prompt the model
sees, for both the cloud and offline paths (one NOVA, not two), and a
capability that appears or disappears from the real registry must be
reflected the next time the summary is generated.
"""
from __future__ import annotations

import nova
from nova_self_knowledge.generate import slim_summary


def test_the_summary_is_present_in_all_three_prompts():
    assert "## Self-Knowledge" in nova.NOVA_CORE
    assert "## Self-Knowledge" in nova.NOVA_SYSTEM_PROMPT
    assert "## Self-Knowledge" in nova.NOVA_OFFLINE_PROMPT


def test_the_summary_names_real_subagents():
    assert "BrowserAgent" in nova.NOVA_CORE


def test_the_summary_does_not_restate_tool_names():
    """Tools already reach the model as real function-calling schemas;
    restating them in prose would be redundant, not additional grounding."""
    summary = slim_summary()
    assert "open_app" not in summary
    assert "function declarations" in summary.lower()


def test_a_capability_appearing_in_the_registry_is_reflected(monkeypatch):
    import nova_self_knowledge.generate as gen

    def fake_subagents():
        return "| Agent | Type |\n| --- | --- |\n| BrandNewAgent | `new` |"

    monkeypatch.setitem(gen.GENERATORS, "subagents", fake_subagents)
    assert "BrandNewAgent" in gen.slim_summary()


def test_a_capability_removed_from_the_registry_stops_appearing(monkeypatch):
    import nova_self_knowledge.generate as gen

    def without_it():
        return "| Agent | Type |\n| --- | --- |\n| SomeOtherAgent | `x` |"

    monkeypatch.setitem(gen.GENERATORS, "subagents", without_it)
    out = gen.slim_summary()
    assert "BrandNewAgent" not in out
    assert "SomeOtherAgent" in out


def test_a_broken_generator_does_not_crash_the_summary(monkeypatch):
    import nova_self_knowledge.generate as gen

    def broken():
        raise RuntimeError("registry is on fire")

    monkeypatch.setitem(gen.GENERATORS, "subagents", broken)
    out = gen.slim_summary()
    assert "unavailable" in out.lower()
    assert "do not guess" in out.lower()
