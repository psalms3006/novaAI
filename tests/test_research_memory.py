"""NOVA's persistent research knowledge: findings from researching a
topic are stored locally and retrievable later, so asking about the same
topic again does not mean researching it from scratch -- distinct from
remember()'s personal-fact store, which explicitly rejects transient/
current-event text that research findings routinely look like.
"""
from __future__ import annotations

import pytest

from living_memory import LivingMemory


def _memory(tmp_path, mirror=False):
    return LivingMemory(path=str(tmp_path / "living_memory.json"), mirror=mirror)


def test_research_findings_are_stored_and_recallable(tmp_path):
    mem = _memory(tmp_path)
    mem.remember_research(
        "extraterrestrial life",
        "No confirmed evidence of extraterrestrial life exists as of the "
        "research date; ongoing searches focus on biosignatures in "
        "exoplanet atmospheres and radio signal surveys.",
        sources=["https://example.org/seti-overview"],
    )

    hits = mem.recall_research("extraterrestrial life")
    assert hits, "research that was just stored was not found again"
    assert "biosignatures" in hits[0]["text"]
    assert hits[0]["meta"]["topic"] == "extraterrestrial life"
    assert hits[0]["meta"]["sources"] == ["https://example.org/seti-overview"]
    assert hits[0]["type"] == "research"


def test_recall_returns_nothing_for_an_unresearched_topic(tmp_path):
    mem = _memory(tmp_path)
    mem.remember_research("black holes", "Black holes are regions of spacetime...")

    assert mem.recall_research("competitive figure skating history") == []


def test_research_does_not_trip_the_transient_or_sensitive_guards(tmp_path):
    """remember() (the personal-fact path) would reject text describing a
    current event as transient, and could plausibly flag research
    discussing e.g. "the password authentication protocol" as sensitive.
    Research findings must not be filtered by rules meant for personal
    facts about the user."""
    mem = _memory(tmp_path)
    # This phrasing would very plausibly trip remember()'s "describes what
    # is happening right now" transient check.
    rec = mem.remember_research(
        "current events",
        "As of today, the situation is actively developing and reporters "
        "are covering it live.",
    )
    assert rec["type"] == "research"

    rec2 = mem.remember_research(
        "authentication",
        "A password authentication protocol typically hashes credentials "
        "before comparison.",
    )
    assert rec2["type"] == "research"


def test_researching_the_same_topic_again_updates_rather_than_duplicates(tmp_path):
    mem = _memory(tmp_path)
    mem.remember_research("quantum computing", "Quantum computers use qubits.")
    mem.remember_research("quantum computing", "Quantum computers use qubits.")

    hits = mem.recall_research("quantum computing")
    matching = [h for h in hits if h["meta"].get("topic") == "quantum computing"]
    assert len(matching) == 1, (
        f"researching the same topic twice created {len(matching)} records "
        f"instead of updating the one that already existed"
    )


def test_research_is_filed_separately_from_personal_facts(tmp_path):
    """A search for personal facts must not surface research findings,
    and vice versa -- they answer different questions."""
    mem = _memory(tmp_path)
    mem.remember("The user's favourite color is teal.")
    mem.remember_research("color theory", "Teal is a blue-green color.")

    facts_only = mem.search("teal", types=["fact"])
    assert all(r["type"] == "fact" for r in facts_only)

    research_only = mem.search("teal", types=["research"])
    assert all(r["type"] == "research" for r in research_only)


# ── wiring: the task manager is what actually does research ──────────────────

def _research_manager(tmp_path, monkeypatch, result):
    import living_memory
    from task_manager import TaskManager
    mem = _memory(tmp_path)
    monkeypatch.setattr(living_memory, "_singleton", mem)
    manager = TaskManager(path=str(tmp_path / "tasks.json"),
                          tool_executor=lambda tool, args, meta: result)
    return mem, manager


def _run(manager, task):
    import time
    manager.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not task.finished:
            time.sleep(0.02)
    finally:
        manager.stop()


SEARCH_RESULT = ("1. Europa ocean may host life -- Liquid water beneath the ice "
                 "shell makes Europa a leading candidate. https://example.org/europa\n"
                 "2. Enceladus plumes -- Cassini sampled organic molecules. "
                 "https://example.org/enceladus")


def test_a_finished_research_task_is_filed_as_research(tmp_path, monkeypatch):
    mem, manager = _research_manager(tmp_path, monkeypatch, SEARCH_RESULT)
    task = manager.submit("life on icy moons",
                          [{"tool": "web_search", "args": {"query": "life on icy moons"}}])
    _run(manager, task)

    [hit] = mem.recall_research("life on icy moons")
    assert "Europa" in hit["text"]
    assert "https://example.org/europa" in hit["meta"]["sources"]


def test_a_task_that_did_no_searching_is_not_filed_as_research(tmp_path, monkeypatch):
    mem, manager = _research_manager(tmp_path, monkeypatch, "Saved: notes.txt")
    task = manager.submit("write notes",
                          [{"tool": "generate_document", "args": {"title": "n", "content": "x"}}])
    _run(manager, task)

    assert mem.recall_research("write notes") == []


def test_submitting_already_researched_ground_tells_the_model(tmp_path, monkeypatch):
    mem, manager = _research_manager(tmp_path, monkeypatch, SEARCH_RESULT)
    mem.remember_research("life on icy moons", "Europa has a subsurface ocean.")

    reply = manager.exec_command(
        "submit", title="life on icy moons",
        steps=[{"tool": "web_search", "args": {"query": "life on icy moons"}}])

    assert "queued" in reply
    assert "Europa has a subsurface ocean." in reply


# ── a search's output is judged as a search, not by generic keywords ─────────
# Real DDG output verified as UNKNOWN (so every research task ended
# "steps ran but success could not be verified"), results *about* a failure
# verified as FAILURE (stopping the task), and "No results found" matched the
# success keyword "found ".

def _verdict(result):
    from task_manager import Task, TaskManager, TaskStep
    import tempfile, os
    m = TaskManager(path=os.path.join(tempfile.mkdtemp(), "t.json"),
                    tool_executor=lambda *a: "")
    return m._verify_step(Task(title="t", steps=[]),
                          TaskStep(tool="web_search", args={}, result=result))


def _ddg(results):
    from actions.web_search import _format_ddg
    return _format_ddg("q", results)


def test_real_search_results_verify_as_success():
    out = _ddg([{"title": "Europa", "snippet": "Liquid water ocean", "url": "https://e.org"}])
    assert _verdict(out) in ("CONFIRMED_SUCCESS", "LIKELY_SUCCESS")


def test_results_about_a_failure_are_still_a_successful_search():
    out = _ddg([{"title": "Why the launch failed",
                 "snippet": "An error in the valve caused the timeout",
                 "url": "https://e.org"}])
    assert _verdict(out) in ("CONFIRMED_SUCCESS", "LIKELY_SUCCESS")


def test_a_search_that_found_nothing_or_could_not_run_is_a_failure():
    assert _verdict(_ddg([])) == "FAILURE"
    assert _verdict("I couldn't search for that, sir. The network backends "
                    "failed (no results) and no offline knowledge matched.") == "FAILURE"
    assert _verdict("Please provide a search query, sir.") == "FAILURE"


def test_a_search_the_dispatcher_reports_as_broken_is_a_failure():
    assert _verdict("web_search error: connection reset") == "FAILURE"
    assert _verdict("Error: it went wrong") == "FAILURE"
    assert _verdict("refused: web access is off") == "FAILURE"
