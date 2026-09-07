"""Regression tests for the Gemini provider's request/response handling.

Both bugs covered here were observed live, in the same tool-using turn:

  1. "400 INVALID_ARGUMENT: Requests ending with a model turn are not
     supported" — the follow-up round after a tool call ended on a model turn.
  2. "'NoneType' object is not iterable" — candidates[0].content.parts is None
     for candidates with no parts, and it was iterated unguarded.

Together they meant every tool-using request failed on the cloud provider and
silently fell through to the slow local model.
"""
from __future__ import annotations

import types

import pytest

from nova_intelligence import gemini_provider as gp

gtypes = pytest.importorskip("google.genai.types")


# ── role mapping ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("role,expected", [
    ("user", "user"),
    ("assistant", "model"),
    ("model", "model"),
    ("tool", "user"),        # tool output is input TO the model
    ("function", "user"),
    ("anything-else", "user"),
])
def test_role_mapping(role, expected):
    assert gp._gemini_role(role) == expected


# ── trailing-turn normalisation ───────────────────────────────────────────────

def _content(role, text):
    return gtypes.Content(role=role, parts=[gtypes.Part(text=text)])


def test_contents_ending_with_model_turn_get_a_closing_user_turn():
    contents = [_content("user", "open notepad"), _content("model", "ok")]
    out = gp._normalize_contents(contents)
    assert len(out) == 3
    assert out[-1].role == "user"


def test_contents_ending_with_user_turn_are_left_alone():
    contents = [_content("user", "hi")]
    assert gp._normalize_contents(contents) is contents


def test_empty_contents_are_left_alone():
    assert gp._normalize_contents([]) == []


def test_full_tool_followup_shape_does_not_end_on_a_model_turn():
    """The exact shape desk.chat builds after running a tool."""
    messages = [
        {"role": "user", "content": "Open Notepad."},
        {"role": "assistant", "content": "(calling open_app)"},
        {"role": "user", "content": "[open_app result]\nOpened Notepad."},
    ]
    contents = [
        gtypes.Content(role=gp._gemini_role(m["role"]),
                       parts=[gtypes.Part(text=m["content"])])
        for m in messages
    ]
    assert gp._normalize_contents(contents)[-1].role == "user"


# ── response parsing ──────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, candidates=None, text=None, text_raises=False):
        self.candidates = candidates
        self._text = text
        self._text_raises = text_raises

    @property
    def text(self):
        if self._text_raises:
            raise ValueError("no text parts in candidate")
        return self._text


def _candidate(parts):
    return types.SimpleNamespace(content=types.SimpleNamespace(parts=parts))


def test_tool_calls_survive_none_parts():
    """The original crash: content.parts is None."""
    resp = _FakeResponse(candidates=[_candidate(None)])
    assert gp._response_tool_calls(resp) == []


def test_tool_calls_survive_none_content():
    resp = _FakeResponse(candidates=[types.SimpleNamespace(content=None)])
    assert gp._response_tool_calls(resp) == []


def test_tool_calls_survive_no_candidates():
    assert gp._response_tool_calls(_FakeResponse(candidates=None)) == []
    assert gp._response_tool_calls(_FakeResponse(candidates=[])) == []


def test_tool_calls_are_extracted_when_present():
    fc = types.SimpleNamespace(name="open_app", args={"app": "notepad"})
    part = types.SimpleNamespace(function_call=fc)
    resp = _FakeResponse(candidates=[_candidate([part])])
    assert gp._response_tool_calls(resp) == [
        {"name": "open_app", "args": {"app": "notepad"}}
    ]


def test_text_parts_without_function_calls_yield_no_tool_calls():
    part = types.SimpleNamespace(function_call=None)
    resp = _FakeResponse(candidates=[_candidate([part])])
    assert gp._response_tool_calls(resp) == []


def test_response_text_survives_a_raising_accessor():
    assert gp._response_text(_FakeResponse(text_raises=True)) == ""


def test_response_text_normalises_none_to_empty_string():
    assert gp._response_text(_FakeResponse(text=None)) == ""


def test_response_text_returns_the_text():
    assert gp._response_text(_FakeResponse(text="hello")) == "hello"
