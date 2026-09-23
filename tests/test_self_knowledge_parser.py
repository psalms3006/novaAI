"""Round-trip stability for the self-knowledge doc's AUTO-block parser.

The one invariant that matters: parsing and re-rendering with no
overrides must reproduce the original text exactly. If that ever breaks,
regenerating one AUTO block would corrupt hand-written prose elsewhere in
the doc, which defeats the entire point of separating the two.
"""
from __future__ import annotations

import pytest

from nova_self_knowledge.parser import (
    Block, MalformedDocError, block_names, detect_line_ending,
    get_auto_content, parse, render,
)


SAMPLE = """# NOVA

Some hand-written identity text.

<!-- AUTO-START: capabilities -->
- old_tool: does a thing
<!-- AUTO-END: capabilities -->

More hand-written prose in between two blocks.

<!-- AUTO-START: integrations -->
- Gemini
<!-- AUTO-END: integrations -->

Trailing hand-written notes.
"""


def test_round_trip_with_no_overrides_is_a_noop():
    blocks = parse(SAMPLE)
    assert render(blocks) == SAMPLE


def test_hand_written_prose_survives_an_auto_block_regeneration():
    blocks = parse(SAMPLE)
    out = render(blocks, {"capabilities": "- new_tool: does a new thing"})
    assert "Some hand-written identity text." in out
    assert "More hand-written prose in between two blocks." in out
    assert "Trailing hand-written notes." in out
    assert "- new_tool: does a new thing" in out
    assert "old_tool" not in out


def test_regenerating_one_block_does_not_touch_another():
    blocks = parse(SAMPLE)
    out = render(blocks, {"capabilities": "- new_tool: x"})
    assert "- Gemini" in out  # integrations block untouched


def test_block_names_lists_every_auto_block_in_order():
    blocks = parse(SAMPLE)
    assert block_names(blocks) == ["capabilities", "integrations"]


def test_get_auto_content_returns_current_inner_text():
    blocks = parse(SAMPLE)
    assert get_auto_content(blocks, "capabilities") == "- old_tool: does a thing"
    assert get_auto_content(blocks, "nonexistent") is None


def test_a_doc_with_no_auto_blocks_at_all_round_trips():
    text = "# Just prose\n\nNothing auto here.\n"
    assert render(parse(text)) == text


def test_crlf_line_endings_are_detected_and_preserved():
    crlf = SAMPLE.replace("\n", "\r\n")
    assert detect_line_ending(crlf) == "\r\n"
    blocks = parse(crlf)
    out = render(blocks, line_ending="\r\n")
    assert out == crlf


def test_an_unclosed_auto_start_is_rejected():
    bad = "prose\n<!-- AUTO-START: x -->\nunclosed\n"
    with pytest.raises(MalformedDocError):
        parse(bad)


def test_a_stray_auto_end_is_rejected():
    bad = "prose\n<!-- AUTO-END: x -->\nmore\n"
    with pytest.raises(MalformedDocError):
        parse(bad)


def test_a_mismatched_end_name_is_rejected():
    bad = "<!-- AUTO-START: a -->\ncontent\n<!-- AUTO-END: b -->\n"
    with pytest.raises(MalformedDocError):
        parse(bad)


def test_nested_auto_blocks_are_rejected():
    bad = ("<!-- AUTO-START: a -->\n"
          "<!-- AUTO-START: b -->\ncontent\n<!-- AUTO-END: b -->\n"
          "<!-- AUTO-END: a -->\n")
    with pytest.raises(MalformedDocError):
        parse(bad)


def test_an_empty_auto_block_round_trips():
    text = "before\n<!-- AUTO-START: e -->\n<!-- AUTO-END: e -->\nafter\n"
    blocks = parse(text)
    assert render(blocks) == text
