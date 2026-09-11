"""What NOVA carries from one session to the next.

The desktop app remembered nothing across restarts, and there were two
separate reasons. It never offered finished turns to Core's memory extractor
at all — only the terminal did — and the gate in front of that extractor was a
list of twenty literal phrases that discarded almost everything worth keeping.

These tests pin both, and the rule that matters more than either: credentials
do not become less sensitive for having been said out loud.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nova                                  # noqa: F401  (Core loads first)
from desk import live_session as ls
from memory_extra import _should_extract_memory


WORTH_KEEPING = [
    "I always want my exported reports saved as PDF, never Word documents.",
    "Call me Psalms from now on.",
    "Stop using bullet points when you summarise things.",
    "The deadline for the OMNIEL launch is in March.",
    "My sister Ada is visiting next week.",
    "Always ask before deleting anything.",
    "I work best in the early morning.",
    "We use Postgres for everything on this project.",
]

NOT_WORTH_KEEPING = [
    "What is the capital of Nigeria?",
    "yes",
    "thanks",
    "Can you open my documents folder?",
    "What time is it right now?",
    "Who is the president of Nigeria?",
    "open spotify please",
    "Tell me a joke about cats",
    "play some music",
    "close that window",
]


class GateTests:
    pass


def test_standing_instructions_and_preferences_are_kept():
    missed = [t for t in WORTH_KEEPING if not _should_extract_memory(t, "")]
    assert not missed, f"these would never reach the extractor: {missed}"


def test_questions_and_one_off_commands_are_skipped():
    """The gate exists to bound cost, so it has to actually skip things."""
    kept = [t for t in NOT_WORTH_KEEPING if _should_extract_memory(t, "")]
    assert not kept, f"these would each cost an extraction call: {kept}"


def test_very_short_turns_are_skipped():
    for t in ("ok", "yes please", "stop", "go on"):
        assert not _should_extract_memory(t, "")


# ── the session has to offer turns at all ────────────────────────────────────

def test_the_session_offers_finished_turns_to_memory():
    src = inspect.getsource(ls.LiveManager._receiver)
    assert "_remember_turn" in src, (
        "the desktop session never offers anything to memory, so nothing it "
        "is told survives a restart")


def test_memory_work_happens_off_the_event_loop():
    """Deciding what to keep can involve a model call. Audio cannot wait."""
    src = inspect.getsource(ls.LiveManager._receiver)
    window = src[src.index("_remember_turn") - 400:src.index("_remember_turn") + 200]
    assert "Thread" in window, "memory extraction would block the audio loop"


# ── secrets ──────────────────────────────────────────────────────────────────

def test_credentials_are_never_offered_to_memory(monkeypatch):
    offered = []
    monkeypatch.setattr(
        ls, "_log", lambda *a, **k: None)

    import memory_extra
    monkeypatch.setattr(memory_extra, "extract_memory_updates",
                        lambda u, a, m: offered.append(u))

    for secret in (
        "My password for the bank account is hunter2 and don't forget it.",
        "The API key is AQ.abcdef123456, remember that for later.",
        "My credit card number is 4111 1111 1111 1111, save it.",
        "Remember my SSN is 123 45 6789 for the forms.",
        "The private key lives in that file, always use it.",
    ):
        ls._remember_turn(secret, "Noted.")
    assert offered == [], f"a credential was sent for storage: {offered}"


def test_ordinary_preferences_are_still_offered(monkeypatch):
    offered = []
    import memory_extra
    monkeypatch.setattr(memory_extra, "extract_memory_updates",
                        lambda u, a, m: offered.append(u))
    monkeypatch.setattr(memory_extra, "load_memory", lambda: {})

    ls._remember_turn(
        "I always want my exported reports saved as PDF, never Word documents.",
        "Understood.")
    assert offered, "a clear standing preference was withheld from memory"


def test_a_broken_extractor_never_reaches_the_caller(monkeypatch):
    """Memory is an enhancement. It must not take a conversation down."""
    import memory_extra

    def boom(*a, **k):
        raise RuntimeError("no network")

    monkeypatch.setattr(memory_extra, "extract_memory_updates", boom)
    monkeypatch.setattr(memory_extra, "load_memory", lambda: {})
    ls._remember_turn("I prefer short answers to long ones.", "Noted.")


# ── the user can see and remove what is kept ─────────────────────────────────

def test_memory_is_inspectable_and_removable():
    """Automatic does not mean opaque. There has to be a way to look."""
    from desk import bridge
    rules = {r.rule for r in bridge.app.url_map.iter_rules()}
    assert "/api/memory" in rules, "no way to see what NOVA has remembered"
    assert "/api/memory/records" in rules, "no way to remove a memory"
    assert "/api/memory/clear" in rules
