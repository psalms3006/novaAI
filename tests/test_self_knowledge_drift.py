"""The drift checker must catch a stale reference (a renamed/deleted file,
a symbol that no longer exists) and must not false-positive on ordinary
prose, flags, or things it cannot verify.
"""
from __future__ import annotations

from nova_self_knowledge.drift import check


def test_a_missing_file_reference_is_flagged():
    doc = "See `this/file/does/not/exist.py` for details.\n"
    findings = check(doc)
    assert len(findings) == 1
    assert findings[0].kind == "missing_file"
    assert "does/not/exist.py" in findings[0].reference


def test_a_real_file_reference_is_not_flagged():
    doc = "See `nova.py` for the entry point.\n"
    assert check(doc) == []


def test_a_missing_symbol_is_flagged():
    doc = "See `nova_agents.this_function_does_not_exist` for details.\n"
    findings = check(doc)
    assert len(findings) == 1
    assert findings[0].kind == "missing_symbol"


def test_a_real_symbol_is_not_flagged():
    doc = "See `nova_agents.get_all_agents` for the registry.\n"
    assert check(doc) == []


def test_a_bare_flag_or_word_is_not_flagged():
    doc = "Set `--refresh` or check the `speaking` state.\n"
    assert check(doc) == []


def test_auto_block_content_is_never_scanned():
    """Only hand-written prose is checked -- an AUTO block can legitimately
    reference something that existed when it was last generated and no
    longer does; that is what --refresh is for, not the drift checker."""
    doc = ("prose\n"
          "<!-- AUTO-START: capabilities -->\n"
          "See `totally/fake/path.py`\n"
          "<!-- AUTO-END: capabilities -->\n")
    assert check(doc) == []


def test_an_allowlisted_reference_is_not_flagged(tmp_path, monkeypatch):
    import nova_self_knowledge.drift as drift_mod
    allowlist = tmp_path / "allow.txt"
    allowlist.write_text("fake/path.py\n", encoding="utf-8")
    monkeypatch.setattr(drift_mod, "ALLOWLIST_PATH", allowlist)

    doc = "See `fake/path.py` (not real yet, planned).\n"
    assert check(doc) == []


def test_a_deliberately_broken_reference_is_caught_and_a_restored_one_passes():
    """The framework's own required check: break a reference, confirm
    --check --strict fails, restore it, confirm it passes again."""
    broken2 = "See `desk/this_file_was_renamed.py` for the loop.\n"
    findings = check(broken2)
    assert len(findings) == 1

    restored = "See `desk/live_session.py` for the loop.\n"
    assert check(restored) == []
