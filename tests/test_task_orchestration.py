"""NOVA as orchestrator: background work that is visible, handed between
agents, reviewed, and honest about how it ended.

What prompted each group, from the real app on 2026-09-26:

* Every research-and-write task ended "partially completed, 1 step(s) not
  verified: generate_document". The document had been written; the verifier
  looked for "saved:" and the tool says "Saved <path> (N bytes)."
* The document it wrote held "..." -- the model planned the writing step
  before any research existed, and nothing passed the findings forward.
* The Agents panel said RESEARCH standby while research ran: only the typed
  chat path wrote agent state, and the rows it did light never went out.
* A step that failed once and succeeded on retry still failed the task.
"""
from __future__ import annotations

import threading
import time
import types

import pytest

import agent_activity
from task_manager import MAX_REVIEW_ROUNDS, TERMINAL, Task, TaskManager


@pytest.fixture(autouse=True)
def _clean_registry():
    agent_activity.reset()
    agent_activity.set_observer(None)
    yield
    agent_activity.reset()
    agent_activity.set_observer(None)


def _manager(tmp_path, executor, **kw):
    return TaskManager(path=str(tmp_path / "tasks.json"), tool_executor=executor, **kw)


def _settle(m, *tasks, timeout=15.0):
    m.start()
    try:
        deadline = time.time() + timeout
        while any(t.status not in TERMINAL for t in tasks) and time.time() < deadline:
            time.sleep(0.02)
        assert all(t.status in TERMINAL for t in tasks), [t.status for t in tasks]
        return tasks
    finally:
        m.stop()


class Writer:
    """generate_document, for real: writes args['content'] to a file and
    reports it the way actions/generate_document.py does."""

    def __init__(self, folder):
        self.folder, self.calls = folder, []

    def __call__(self, tool, args, meta):
        if tool == "web_search":
            return (f"Results for {args['query']}:\n1. Finding about {args['query']} "
                    f"— https://example.org/{len(self.calls)}")
        self.calls.append(dict(args))
        path = self.folder / f"{args.get('title', 'doc')}.md"
        path.write_text(args.get("content", ""), encoding="utf-8")
        return f"Saved {path} ({path.stat().st_size} bytes)."


RESEARCH_THEN_WRITE = [
    {"tool": "web_search", "args": {"query": "thesis sources"}},
    {"tool": "web_search", "args": {"query": "thesis methods"}},
    {"tool": "generate_document", "args": {"title": "thesis", "content": "..."}},
]


def _steps():
    return [{"tool": s["tool"], "args": dict(s["args"])} for s in RESEARCH_THEN_WRITE]


# ── verification against the disk ───────────────────────────────────────────

def test_a_written_document_verifies_and_the_task_completes(tmp_path):
    m = _manager(tmp_path, Writer(tmp_path))
    [t] = _settle(m, m.submit("Thesis", _steps()))
    assert t.status == "COMPLETED", t.reason_for_stop
    assert t.steps[2].verification == "CONFIRMED_SUCCESS"
    [art] = t.artifacts
    assert art["path"].endswith("thesis.md") and art["size"] > 0 and art["agent"] == "creative"


def test_a_claimed_file_that_is_not_there_is_a_failure(tmp_path):
    m = _manager(tmp_path, lambda tool, args, meta: f"Saved {tmp_path / 'ghost.md'} (120 bytes).")
    [t] = _settle(m, m.submit("Ghost", [{"tool": "generate_document",
                                         "args": {"title": "g", "content": "x"}}]))
    assert t.status == "FAILED"
    assert t.steps[0].verification == "FAILURE"


def test_a_step_that_succeeds_on_retry_does_not_fail_the_task(tmp_path):
    calls = []

    def flaky(tool, args, meta):
        calls.append(tool)
        if len(calls) == 1:
            raise ConnectionError("network blip")
        return "Results: found it"

    m = _manager(tmp_path, flaky)
    [t] = _settle(m, m.submit("Flaky", [{"tool": "web_search", "args": {"query": "x"}}]))
    assert calls == ["web_search", "web_search"]
    assert t.steps[0].status == "VERIFIED" and t.steps[0].retries == 1
    assert t.status == "COMPLETED", t.reason_for_stop
    assert any(h["event"] == "task.retry" for h in t.history)


def test_failure_words_deep_inside_a_result_do_not_fail_it(tmp_path):
    m = _manager(tmp_path, lambda *a: "")
    step = types.SimpleNamespace(tool="app_control", args={},
                                 result="Opened Notepad. " + "x" * 300 + " the earlier error: none")
    assert m._verify_step(Task(title="t"), step) == "CONFIRMED_SUCCESS"


# ── step output flows forward, as a message between agents ─────────────────

def test_the_document_is_written_from_the_research(tmp_path):
    writer = Writer(tmp_path)
    m = _manager(tmp_path, writer)
    [t] = _settle(m, m.submit("Thesis", _steps()))
    body = writer.calls[0]["content"]
    assert body.strip() != "..."
    assert "Finding about thesis sources" in body and "Finding about thesis methods" in body
    [handoff] = [x for x in t.agent_messages if x["kind"] == "handoff"]
    assert (handoff["from"], handoff["to"], handoff["step"]) == ("research", "creative", 2)
    assert handoff["data"]["from_steps"] == [0, 1]


def test_every_step_is_assigned_and_answered(tmp_path):
    m = _manager(tmp_path, Writer(tmp_path))
    [t] = _settle(m, m.submit("Thesis", _steps()))
    assigns = [x for x in t.agent_messages if x["kind"] == "assign"]
    results = [x for x in t.agent_messages if x["kind"] == "result"]
    assert [a["to"] for a in assigns] == ["research", "research", "creative"]
    assert [r["from"] for r in results] == ["research", "research", "creative"]
    assert all(a["from"] == "orchestrator" for a in assigns)


# ── the reviewer sends problems back to the agent responsible ───────────────

def test_review_feedback_reaches_the_responsible_agent_and_is_acted_on(tmp_path, monkeypatch):
    m = _manager(tmp_path, Writer(tmp_path))
    real = TaskManager._review
    rounds = []

    def review(self, t, rnd):
        rounds.append(rnd)
        if rnd == 1:
            return {"round": 1, "passed": False, "checked": ["x"], "ts": time.time(),
                    "issues": [{"step": 2, "agent": "creative",
                                "problem": "the document does not use the research",
                                "fix": "write it from the research findings"}]}
        return real(self, t, rnd)

    monkeypatch.setattr(TaskManager, "_review", review)
    [t] = _settle(m, m.submit("Thesis", _steps()))
    assert rounds == [1, 2]
    [fb] = [x for x in t.agent_messages if x["kind"] == "review_feedback"]
    assert (fb["from"], fb["to"], fb["step"]) == ("reviewer", "creative", 2)
    assert len(t.reviews) == 2 and t.reviews[-1]["passed"]
    assert t.status == "COMPLETED"
    # the writer was assigned the step again after the feedback
    assert len([x for x in t.agent_messages if x["kind"] == "assign" and x["step"] == 2]) == 2


def test_the_review_loop_is_bounded(tmp_path, monkeypatch):
    m = _manager(tmp_path, Writer(tmp_path))
    monkeypatch.setattr(TaskManager, "_review", lambda self, t, rnd: {
        "round": rnd, "passed": False, "checked": [], "ts": time.time(),
        "issues": [{"step": 2, "agent": "creative", "problem": "never good enough",
                    "fix": "write it again"}]})
    [t] = _settle(m, m.submit("Thesis", _steps()))
    assert len(t.reviews) == MAX_REVIEW_ROUNDS + 1
    assert t.status == "PARTIALLY_COMPLETED"
    assert "never good enough" in t.reason_for_stop


def test_a_document_with_no_body_and_no_research_is_not_passed(tmp_path):
    m = _manager(tmp_path, Writer(tmp_path))
    [t] = _settle(m, m.submit("Empty", [{"tool": "generate_document",
                                         "args": {"title": "e", "content": "..."}}]))
    assert t.status == "PARTIALLY_COMPLETED"
    assert "no real content" in t.reason_for_stop
    # With no findings to write it from, sending it back would only repeat it.
    assert not [x for x in t.agent_messages if x["kind"] == "review_feedback"]
    assert len(t.reviews) == 1


def test_links_in_findings_are_not_taken_for_files():
    """"//example.org/x" out of a URL is a network share to Windows; checking
    it stalled a finished research task in RUNNING."""
    from task_manager import _extract_paths
    found = _extract_paths("see https://example.org/0 and file://host/share and /tmp/abc.txt")
    assert found == ["/tmp/abc.txt"]


def test_a_writer_claiming_a_file_it_never_wrote_stops_the_task(tmp_path):
    class Liar(Writer):
        def __call__(self, tool, args, meta):
            if tool == "generate_document":
                return f"Saved {self.folder / 'missing.md'} (10 bytes)."
            return super().__call__(tool, args, meta)

    m = _manager(tmp_path, Liar(tmp_path))
    [t] = _settle(m, m.submit("Thesis", _steps()))
    assert t.status == "FAILED" and "generate_document" in t.reason_for_stop


# ── one source of truth for agent state ──────────────────────────────────────

def _row(agent):
    return next(a for a in agent_activity.snapshot() if a["id"] == agent)


def test_research_shows_as_running_while_a_task_researches(tmp_path):
    inside, release = threading.Event(), threading.Event()

    def slow(tool, args, meta):
        inside.set()
        release.wait(5)
        return "Results: something"

    m = _manager(tmp_path, slow)
    t = m.submit("Look it up", [{"tool": "web_search", "args": {"query": "exoplanets"}}])
    m.start()
    try:
        assert inside.wait(5)
        row = _row("research")
        assert row["state"] == "running"
        assert row["task_id"] == t.id and "exoplanets" in row["action"]
        assert t.status == "RUNNING" and t.phase == "Search: exoplanets"
        d = t.to_dict()
        assert d["current"] == "Search: exoplanets" and d["steps_total"] == 1
    finally:
        release.set()
        _settle(m, t)
    assert _row("research")["state"] == "standby"


def test_every_agent_row_goes_out_when_its_work_ends(tmp_path):
    m = _manager(tmp_path, Writer(tmp_path))
    _settle(m, m.submit("Thesis", _steps()))
    assert all(a["state"] == "standby" for a in agent_activity.snapshot())


def test_tools_map_to_the_agent_that_owns_them():
    f = agent_activity.agent_for_tool
    assert f("web_search") == "research"
    assert f("generate_document") == "creative"
    assert f("browser_control") == "browser"
    assert f("computer_control") == "computer" and f("file_controller") == "computer"
    assert f("nova_task") == "orchestrator" and f("something_new") == "orchestrator"


def test_the_chat_paths_agent_ids_map_to_the_right_rows():
    """publish_agent_done matched by substring: 'research' is not in
    'agent-web_search-3f9c', so the row it lit stayed lit until restart.
    Both ends now use the same key."""
    import inspect
    from desk import bridge
    assert bridge._agent_key("agent-web_search-3f9c") == "research"
    assert bridge._agent_key("agent-generate_document-3f9c") == "creative"
    assert bridge._agent_key("thinking-3f9c") == "orchestrator"
    src = inspect.getsource(bridge.run_desk_server)
    assert 'key=f"chat:{agent_id}"' in src and 'agent_activity.end(f"chat:{agent_id}")' in src


def test_the_system_snapshot_reads_the_registry(monkeypatch, tmp_path):
    from desk import bridge
    m = _manager(tmp_path, lambda *a: "ok")
    monkeypatch.setattr(bridge, "_ns", lambda name, default=None:
                        m if name == "_task_manager" else default)
    token = agent_activity.begin("research", "Search: comets", source="voice")
    try:
        body = bridge.app.test_client().get(
            "/api/system", headers={"X-NOVA-Desk": bridge.run_token}).get_json()
    finally:
        agent_activity.end(token)
    research = next(a for a in body["agents"] if a["id"] == "research")
    assert research["state"] == "running" and research["action"] == "Search: comets"
    assert {a["id"] for a in body["agents"]} >= {"orchestrator", "research", "browser",
                                                  "computer", "creative", "reviewer"}


def test_a_voice_tool_call_lights_its_agent(monkeypatch):
    """The live session runs most tools, and never touched agent state."""
    import asyncio
    from desk import live_session as ls
    seen = {}

    def execute(name, args):
        seen["during"] = _row("research")["state"]
        return "Results: ok"

    mgr = ls.LiveManager()
    mgr._publish = lambda ev: None
    monkeypatch.setattr(ls.LiveManager, "_execute_tool", staticmethod(execute))
    fc = types.SimpleNamespace(name="web_search", args={"query": "q"}, id="1")
    asyncio.run(mgr._run_tool(fc))
    assert seen["during"] == "running"
    assert _row("research")["state"] == "standby"


# ── events ───────────────────────────────────────────────────────────────────

def test_the_work_is_published_as_events(tmp_path):
    events = []
    m = _manager(tmp_path, Writer(tmp_path))
    m.set_on_event(events.append)
    agent_activity.set_observer(events.append)
    [t] = _settle(m, m.submit("Thesis", _steps()))
    kinds = [e["type"] for e in events]
    for expected in ("task.created", "task.started", "task.step", "agent.message",
                     "agent.state", "task.progress", "artifact.created",
                     "review.completed", "task.completed"):
        assert expected in kinds, f"no {expected} event"
    assert kinds.index("task.created") < kinds.index("task.started") < kinds.index("task.completed")
    assert all(e["task_id"] == t.id for e in events if e["type"].startswith("task."))


# ── concurrency and shared resources ────────────────────────────────────────

def test_two_tasks_never_drive_the_desktop_at_once(tmp_path):
    active, overlaps, lock = [0], [], threading.Lock()

    def desktop(tool, args, meta):
        with lock:
            active[0] += 1
            overlaps.append(active[0])
        time.sleep(0.3)
        with lock:
            active[0] -= 1
        return "Opened it"

    m = _manager(tmp_path, desktop)
    waited = []
    m.set_on_event(lambda e: waited.append(e["task_id"]) if e["type"] == "task.waiting" else None)
    a = m.submit("A", [{"tool": "computer_control", "args": {"action": "click"}}])
    b = m.submit("B", [{"tool": "computer_control", "args": {"action": "type"}}])
    _settle(m, a, b)
    assert max(overlaps) == 1, "two tasks used the desktop at the same time"
    assert waited, "the second task never said it was waiting for the desktop"
    assert a.status == b.status == "COMPLETED"


def test_the_voice_session_waits_for_the_desktop_too(monkeypatch):
    """Resource safety covers the dispatcher, not only the task pool."""
    import nova
    lock = agent_activity.resource_lock("desktop")
    holder_in, holder_out = threading.Event(), threading.Event()

    def hold():
        with lock:
            holder_in.set()
            holder_out.wait(5)

    th = threading.Thread(target=hold)
    th.start()
    assert holder_in.wait(5)
    try:
        real_acquire = lock.acquire
        monkeypatch.setattr(agent_activity, "resource_lock",
                            lambda name: types.SimpleNamespace(
                                acquire=lambda timeout=None: real_acquire(timeout=0.2),
                                release=lock.release))
        out = nova._execute_tool_sync("computer_control", {"action": "click"}, {})
        assert "being used by a background task" in out
    finally:
        holder_out.set()
        th.join()


def test_research_tasks_do_run_side_by_side(tmp_path):
    active, peak, lock = [0], [0], threading.Lock()

    def search(tool, args, meta):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.3)
        with lock:
            active[0] -= 1
        return "Results: x"

    m = _manager(tmp_path, search)
    tasks = [m.submit(f"R{i}", [{"tool": "web_search", "args": {"query": str(i)}}]) for i in range(2)]
    _settle(m, *tasks)
    assert peak[0] == 2


# ── failure handling ────────────────────────────────────────────────────────

def test_a_task_can_be_cancelled_while_it_runs(tmp_path):
    started = threading.Event()

    def slow(tool, args, meta):
        started.set()
        time.sleep(0.3)
        return "Results: x"

    m = _manager(tmp_path, slow)
    t = m.submit("Long", [{"tool": "web_search", "args": {"query": str(i)}} for i in range(5)])
    m.start()
    assert started.wait(5)
    assert m.cancel(t.id)
    _settle(m, t)
    assert t.status == "CANCELLED"
    assert sum(1 for s in t.steps if s.status == "VERIFIED") < 5


def test_a_failed_task_can_be_retried_as_a_new_task(tmp_path):
    calls = []

    def once_bad(tool, args, meta):
        calls.append(1)
        return "Refused: not now." if len(calls) == 1 else "Results: x"

    m = _manager(tmp_path, once_bad)
    [first] = _settle(m, m.submit("R", [{"tool": "web_search", "args": {"query": "x"}}]))
    assert first.status == "FAILED"
    second = m.retry(first.id)
    assert second is not None and second.parent_task_id == first.id
    _settle(m, second)
    assert second.status == "COMPLETED"
    assert m.retry(second.id) is None, "a completed task has nothing to retry"


def test_a_task_interrupted_by_a_restart_is_not_left_running(tmp_path):
    m = _manager(tmp_path, lambda *a: "ok")
    t = m.submit("R", [{"tool": "web_search", "args": {"query": "x"}}])
    t.status = "RUNNING"
    m._save()
    [reloaded] = _manager(tmp_path, lambda *a: "ok").list()
    assert reloaded.status == "FAILED" and "closed" in reloaded.reason_for_stop


def test_the_voice_is_told_once_at_the_end_not_during(tmp_path):
    told = []
    m = _manager(tmp_path, Writer(tmp_path))
    m.set_notify(told.append)
    _settle(m, m.submit("Thesis", _steps()))
    # The announcement follows the terminal status on the worker thread.
    deadline = time.time() + 5
    while not told and time.time() < deadline:
        time.sleep(0.02)
    assert len(told) == 1 and "is done" in told[0] and "thesis.md" in told[0]
