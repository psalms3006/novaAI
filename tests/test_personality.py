"""NOVA's personality layer (nova_personality) and how it is wired.

These pin structure and deterministic behaviour. Whether a model actually
behaves like NOVA is measured separately, against real models, by
tests/eval/personality_eval.py.
"""
from __future__ import annotations

import json

import pytest

import nova_personality as P


@pytest.fixture()
def prefs_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVA_DATA_DIR", str(tmp_path))
    return tmp_path


# -- Layer A --------------------------------------------------------------------

def test_every_mode_carries_the_same_core_policies():
    for mode in ("text", "voice"):
        r = P.render(mode)
        for section in ("Confidence follows the evidence", "Agreeing and disagreeing",
                        "Corrections", "How you sound", "Working with your agents",
                        "Truth comes first", "Examples of you"):
            assert section in r, (mode, section)
    off = P.render("offline").lower()
    assert "i don't know" in off and "disagree" in off and "offline" in off


def test_voice_is_shaped_for_the_ear_and_text_for_the_page():
    assert "No lists, headings" in P.render("voice")
    assert "No lists, headings" not in P.render("text")
    assert "In writing" in P.render("text")


def test_the_identity_is_original_not_borrowed():
    everything = " ".join(P.render(m) for m in ("text", "voice", "offline")).lower()
    for name in ("zoey", "trillion"):
        assert name not in everything


def test_agents_have_temperaments_and_answer_to_nova():
    for role in ("research", "browser", "computer", "coding", "reviewer", "planner"):
        r = P.render(f"agent:{role}")
        assert "NOVA checks your work" in r and P.AGENT_TEMPERAMENTS[role] in r


def test_anchors_show_behaviour_not_catchphrases():
    anchors = P.render("text").split("## Examples of you", 1)[1]
    replies = [l for l in anchors.splitlines() if l.startswith("NOVA:")]
    assert len(replies) >= 5
    openers = [" ".join(r.split()[1:3]).lower() for r in replies]
    assert len(set(openers)) == len(openers), "every example must open differently"
    for bad in ("great question", "absolutely", "certainly", "happy to help"):
        assert bad not in anchors.lower()


# -- Layer B --------------------------------------------------------------------

@pytest.mark.parametrize("said,key,expected", [
    ("Can you keep it shorter please", "verbosity", -1),
    ("your answers are too long", "verbosity", -1),
    ("go deeper on this, more detail", "verbosity", 1),
    ("no jokes, I'm not in the mood", "humour", 0),
    ("tell me when I'm wrong, don't be polite about it", "challenge", 2),
    ("just do what I ask, stop arguing", "challenge", 0),
])
def test_explicit_requests_adapt_nova(prefs_dir, said, key, expected):
    P.learn_from_message(said)
    assert P.load_prefs()[key] == expected


def test_ordinary_conversation_does_not_change_anything(prefs_dir):
    for said in ("what's the weather in Lagos", "open notepad", "the report is long but good",
                 "I'm a bit sad today", "can you question the witness list in my notes"):
        assert P.learn_from_message(said) == []
    assert not (prefs_dir / "personality_prefs.json").exists()


def test_adaptation_is_bounded_and_keeps_the_honesty_floor(prefs_dir):
    for _ in range(6):
        P.learn_from_message("just do it, don't argue")
        P.learn_from_message("keep it shorter")
    p = P.load_prefs()
    assert p["challenge"] == 0 and p["verbosity"] == -2
    block = P.adaptation_block()
    # Even at the floor, real risks and false premises are still flagged.
    assert "real risks, false premises" in block


def test_adaptation_is_per_account(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVA_DATA_DIR", str(tmp_path / "alice"))
    P.learn_from_message("no jokes")
    monkeypatch.setenv("NOVA_DATA_DIR", str(tmp_path / "bob"))
    assert P.load_prefs()["humour"] == 1


def test_the_evidence_for_a_change_is_kept_and_bounded(prefs_dir):
    for i in range(30):
        P.learn_from_message("be shorter" if i % 2 else "more detail please")
    ev = json.loads((prefs_dir / "personality_prefs.json").read_text())["evidence"]
    assert len(ev) == 20 and ev[-1]["said"]


def test_response_style_setting_folds_into_layer_b(prefs_dir):
    assert "shorter" in P.adaptation_block("concise").lower()
    assert P.adaptation_block("balanced") == ""


# -- the deterministic guard ------------------------------------------------------

@pytest.mark.parametrize("raw,clean", [
    ("Great question! The capital is Canberra.", "The capital is Canberra."),
    ("Absolutely! I'll open it now.", "I'll open it now."),
    ("Certainly. Here's the file.", "Here's the file."),
    ("I'd be happy to help with that! first, the queue.", "First, the queue."),
    ("As an AI language model, I can't taste food.", "I can't taste food."),
    ("That's not certainly true.", "That's not certainly true."),
    ("Absolutely not — that deletes your backups.", "Absolutely not — that deletes your backups."),
    ("Absolutely!", "Absolutely!"),
])
def test_filler_openers_are_removed_and_nothing_else(raw, clean):
    assert P.clean_reply(raw) == clean


def test_the_streaming_filter_cleans_the_opening_of_each_reply():
    from desk.bridge import _persona_filter
    events = [{"type": "token", "text": "Great "}, {"type": "token", "text": "question! It's "},
              {"type": "token", "text": "Canberra."}, {"type": "tool_start", "name": "x"},
              {"type": "token", "text": "Absolutely! Done."},
              {"type": "assistant", "text": "Great question! It's Canberra. Absolutely! Done."}]
    out = list(_persona_filter(iter(events)))
    streamed = "".join(e["text"] for e in out if e["type"] == "token")
    assert "Great question" not in streamed and "Absolutely!" not in streamed
    assert "Canberra." in streamed and "Done." in streamed
    final = [e for e in out if e["type"] == "assistant"][0]["text"]
    assert final.startswith("It's Canberra.")
    assert any(e["type"] == "tool_start" for e in out)


# -- NOVA's voice for background work -------------------------------------------------

def test_a_finished_task_is_told_as_nova_not_as_a_status_line():
    m = P.task_outcome_message("Thesis", "COMPLETED", "", [{"path": "C:/x/thesis.md"}], [])
    assert m == "“Thesis” is done. It's saved as thesis.md."


def test_review_catches_are_reported_not_hidden():
    reviews = [{"passed": False, "issues": [{"problem": "the document has no real content"}]},
               {"passed": True, "issues": []}]
    m = P.task_outcome_message("Report", "COMPLETED", "", [{"path": "r.docx"}], reviews)
    assert "didn't hold up (the document has no real content)" in m and "checks out" in m


def test_failures_and_unverified_work_are_never_softened():
    assert P.task_outcome_message("X", "FAILED", "network down").startswith("I couldn't finish")
    assert "wouldn't rely on it" in P.task_outcome_message("X", "PARTIALLY_COMPLETED", "step 2 empty")
    assert "couldn't confirm" in P.task_outcome_message("X", "UNVERIFIED", "")


# -- wiring ---------------------------------------------------------------------------

def test_typed_chat_now_knows_who_it_is_talking_to(prefs_dir, monkeypatch):
    from desk import bridge
    monkeypatch.setattr(bridge.desk_live, "_identity_block",
                        lambda: "[WHO YOU ARE TALKING TO]\nYou are talking to Psalms.")
    P.learn_from_message("no jokes")
    prompt = bridge._system_prompt("hello", {"user_name": "Psalms"}, None)
    assert "You are talking to Psalms." in prompt
    assert "No jokes or banter" in prompt
    assert "## Agreeing and disagreeing" in prompt


def test_the_voice_prompt_is_the_spoken_variant():
    import nova
    assert "## Out loud" in nova.NOVA_VOICE_PROMPT
    assert "## Out loud" not in nova.NOVA_SYSTEM_PROMPT
