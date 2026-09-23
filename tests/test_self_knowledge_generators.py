"""Generators must read from the same registration site the runtime uses,
not a re-listed copy -- and must degrade to a clearly-marked placeholder
rather than raise when their source is unavailable.
"""
from __future__ import annotations

import textwrap

from nova_self_knowledge import generators as g


def test_capabilities_reads_the_real_tool_declarations():
    """Smoke test against the actual repo: nova.py really has
    TOOL_DECLARATIONS, and the generator finds every tool in it."""
    out = g.generate_capabilities()
    assert "_unavailable" not in out
    assert "open_app" in out
    assert "web_search" in out
    assert out.count("|") > 10  # a real table, not an empty shell


def test_capabilities_parses_a_fixture_without_importing_it(tmp_path):
    fixture = tmp_path / "fake_nova.py"
    fixture.write_text(textwrap.dedent('''
        TOOL_DECLARATIONS = [
            {"name": "do_thing", "description": "Does the thing. Extra detail.",
             "parameters": {}},
            {"name": "other_thing", "description": "Does another thing.",
             "parameters": {}},
        ]
    '''), encoding="utf-8")
    out = g.generate_capabilities(fixture)
    assert "do_thing" in out
    assert "Does the thing" in out
    assert "Extra detail" not in out  # only the first sentence is kept
    assert "other_thing" in out
    assert "2 tools declared" in out


def test_capabilities_reports_unavailable_for_a_missing_file(tmp_path):
    out = g.generate_capabilities(tmp_path / "does_not_exist.py")
    assert "_unavailable" in out


def test_capabilities_reports_unavailable_when_the_list_is_absent(tmp_path):
    fixture = tmp_path / "empty.py"
    fixture.write_text("X = 1\n", encoding="utf-8")
    out = g.generate_capabilities(fixture)
    assert "_unavailable" in out


def test_subagents_reads_the_real_registry_without_a_circular_import_error():
    """agents_extra.py imports nova, and nova imports agents_extra -- a
    generator that needs only class names must not trip that."""
    out = g.generate_subagents()
    assert "_unavailable" not in out
    assert "BrowserAgent" in out
    assert "OrchestratorAgent" in out  # from agents_extra.py's second wave


def test_integrations_always_returns_a_well_formed_table():
    out = g.generate_integrations()
    assert out.startswith("| Integration | Purpose | Status |")
    assert "Gemini" in out
    assert "Ollama" in out


def test_recent_activity_reads_real_git_log():
    out = g.generate_recent_activity(days=365)
    assert "_unavailable" not in out
    assert "Commits in the last 365 days" in out


def test_recent_activity_handles_a_window_with_nothing_in_it():
    out = g.generate_recent_activity(days=0)
    # A 0-day window may have no commits or the exact same-second HEAD --
    # either is a valid, non-crashing answer.
    assert "_unavailable" not in out


def test_voice_loop_names_both_real_paths():
    out = g.generate_voice_loop()
    assert "LiveManager" in out
    assert "run_offline_loop_v2" in out
    assert "VoiceGate" in out


def test_generator_registry_covers_every_generator_function():
    assert set(g.GENERATORS) == {
        "capabilities", "subagents", "integrations", "voice_loop",
        "recent_activity",
    }
