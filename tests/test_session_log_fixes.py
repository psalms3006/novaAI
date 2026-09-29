"""Fixes from reading NOVA's own log of the 2026-09-29 session.

  * a spoken session left nothing behind (archived as "Empty session", no
    conversation in history);
  * typed turns never reached the session archive either;
  * a turn worth remembering was dropped while the model was rate-limited;
  * a memory-extraction 429 refused the whole typed chat for up to 30 min;
  * a tool that needed a "yes" gave up at 30 s while the question was on screen;
  * self-editing in the installed app would have launched NOVA.exe as a test run;
  * PDFs showed literal **asterisks**;
  * a failing tool left no trace in the log.
"""
import asyncio
import json
import logging
import sys
import time
import types

import pytest


class _Mem:
    def __init__(self):
        self.turns = []

    def log_turn(self, role, content):
        self.turns.append((role, content))


# ── voice and typed turns are kept ───────────────────────────────────────────
def test_a_voice_session_becomes_a_conversation_and_reaches_the_archive(monkeypatch):
    import nova
    from desk import live_session as ls
    from desk import store
    mem = _Mem()
    monkeypatch.setattr(nova, "_nova_memory", mem, raising=False)
    holder = {}
    ls._archive_turn(holder, "remind me what we planned for the launch", "We planned March.")
    ls._archive_turn(holder, "and the budget?", "Ten thousand.")
    ls._archive_turn(holder, "", "")                       # nothing said: nothing stored
    convo = store.get_conversation(holder["cid"])
    assert convo["title"].startswith("Voice — remind me what we planned")
    assert [(m["role"], m["content"]) for m in convo["messages"]] == [
        ("user", "remind me what we planned for the launch"), ("assistant", "We planned March."),
        ("user", "and the budget?"), ("assistant", "Ten thousand.")]
    assert all(json.loads(m["meta"]).get("voice") for m in convo["messages"])
    assert mem.turns[0] == ("user", "remind me what we planned for the launch")
    assert len(mem.turns) == 4


def test_each_voice_session_starts_a_new_conversation():
    src = open("desk/live_session.py", encoding="utf-8").read()
    start = src[src.index("    def start(self) -> dict:"):src.index("    def stop(self) -> dict:")]
    assert "self._voice_history = {}" in start


def test_typed_turns_reach_the_session_archive(monkeypatch):
    import nova
    from desk import bridge, store
    mem = _Mem()
    monkeypatch.setattr(nova, "_nova_memory", mem, raising=False)
    cid = store.new_conversation()
    store.add_message(cid, "user", "my sister Ada visits next week", {})
    bridge._persist_turn(cid, "my sister Ada visits next week",
                         [{"type": "assistant", "text": "Noted."}], time.time())
    assert mem.turns == [("user", "my sister Ada visits next week"), ("assistant", "Noted.")]


# ── memory is queued, never dropped ──────────────────────────────────────────
@pytest.fixture
def memx(tmp_path, monkeypatch):
    import memory_extra as mx
    import nova_state
    monkeypatch.setattr(mx, "MEMORY_PENDING_FILE", tmp_path / "memory_pending.jsonl")
    monkeypatch.setattr(mx, "HAS_GEMINI", True)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setattr(nova_state, "_rest_backoff_until", 0.0)
    monkeypatch.setattr(nova_state, "_rest_backoff_secs", 0.0)
    stored = []
    monkeypatch.setattr(mx, "add_memory_fact", lambda text, meta: stored.append(text) or True)
    monkeypatch.setattr(mx, "genai", types.SimpleNamespace(Client=lambda api_key: object()))
    mx._stored = stored
    return mx


def test_a_rate_limited_turn_is_kept_and_remembered_later(memx, monkeypatch):
    import nova_state
    nova_state._rest_backoff_until = time.time() + 600
    memx.extract_memory_updates("I always want my reports as PDF files", "Got it.", {})
    assert memx.pending_count() == 1
    nova_state._rest_backoff_until = 0.0
    models = []

    def fake_generate(client, model, contents):
        models.append(model)
        return types.SimpleNamespace(text='{"new_preference": "reports as PDF"}')
    monkeypatch.setattr(memx, "_gemini_generate_with_delay", fake_generate)
    memx.extract_memory_updates("thanks", "", {})           # any turn drains the queue
    assert memx.pending_count() == 0
    assert memx._stored == ["Preference: reports as PDF"]
    assert models == ["gemini-flash-lite-latest"]           # not the chat model's quota


def test_a_transient_failure_keeps_the_turn(memx, monkeypatch):
    def busy(client, model, contents):
        raise RuntimeError("429 RESOURCE_EXHAUSTED")
    monkeypatch.setattr(memx, "_gemini_generate_with_delay", busy)
    memx.extract_memory_updates("my sister Ada is visiting next week", "Nice.", {})
    assert memx.pending_count() == 1


def test_a_retired_memory_model_falls_back(memx, monkeypatch):
    calls = []

    def gen(client, model, contents):
        calls.append(model)
        if model == memx.MEMORY_MODEL:
            raise RuntimeError("404 NOT_FOUND model")
        return types.SimpleNamespace(text='{"new_fact": "Ada is the user\'s sister"}')
    monkeypatch.setattr(memx, "_gemini_generate_with_delay", gen)
    memx.extract_memory_updates("my sister Ada is visiting next week", "Nice.", {})
    assert calls == [memx.MEMORY_MODEL, memx.VISION_MODEL]
    assert memx._stored == ["Ada is the user's sister"]


def test_the_queue_is_bounded(memx):
    import nova_state
    nova_state._rest_backoff_until = time.time() + 600
    for i in range(memx._PENDING_MAX + 25):
        memx._queue_pending(f"I always do thing number {i}", "")
    assert memx.pending_count() == memx._PENDING_MAX


def test_chat_is_not_refused_because_another_caller_was_rate_limited():
    src = open("desk/chat.py", encoding="utf-8").read()
    block = src[src.index("if _is_rate_limited and callable(_is_rate_limited)"):][:400]
    assert '"error"' not in block and "return" not in block.split("else:")[0]


# ── a "yes" does not race the tool's clock ───────────────────────────────────
def test_waiting_for_the_person_does_not_count_against_the_tool(monkeypatch):
    from desk import live_session as ls
    waiting = {"on": True}
    monkeypatch.setattr(ls, "_confirmation_waiting", lambda: waiting["on"])

    async def scenario():
        async def slow():
            await asyncio.sleep(0.6)
            return "done"
        task = asyncio.ensure_future(slow())
        loop = asyncio.get_running_loop()
        loop.call_later(0.5, lambda: waiting.update(on=False))
        return await ls.LiveManager._await_tool(task, budget=0.2)
    assert asyncio.run(scenario()) == "done"


def test_a_tool_still_times_out_on_its_own_work(monkeypatch):
    from desk import live_session as ls
    monkeypatch.setattr(ls, "_confirmation_waiting", lambda: False)

    async def scenario():
        task = asyncio.ensure_future(asyncio.sleep(5))
        try:
            await ls.LiveManager._await_tool(task, budget=0.2)
        finally:
            task.cancel()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(scenario())


# ── self-editing is honest about where it can run ────────────────────────────
def test_self_editing_refuses_in_the_installed_app(monkeypatch, tmp_path):
    from nova_self import improve
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert "installed app" in improve.unavailable_reason(tmp_path)
    s = improve.SelfImprover(root=tmp_path)
    out = s.rehearse(improve.Proposal("p", [improve.Edit("a.py", "x", "y")]))
    assert out.status == "unavailable" and not out.ok


def test_self_editing_refuses_outside_a_checkout(monkeypatch, tmp_path):
    from nova_self import improve
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert "checkout" in improve.unavailable_reason(tmp_path)


# ── documents render markdown ────────────────────────────────────────────────
def test_pdf_and_docx_render_bold_instead_of_asterisks(tmp_path):
    import docx
    import pypdf
    from actions import generate_document as g
    content = "**Communicate:** talk *naturally* with `tools`.\n- **Files:** read\nsnake_case_name"
    g._write_pdf(tmp_path / "a.pdf", content, "T")
    text = "".join(p.extract_text() for p in pypdf.PdfReader(str(tmp_path / "a.pdf")).pages)
    assert "**" not in text and "`" not in text and "snake_case_name" in text
    g._write_docx(tmp_path / "a.docx", content, "T")
    runs = [(r.text, r.bold) for p in docx.Document(str(tmp_path / "a.docx")).paragraphs for r in p.runs]
    assert ("Communicate:", True) in runs and ("Files:", True) in runs
    assert g._plain("**a** and _b_ and snake_case") == "a and b and snake_case"


# ── a failing tool leaves a trail ────────────────────────────────────────────
def test_tool_failures_are_logged_with_their_class(caplog):
    import nova  # noqa: F401  (installs the hook)
    from nova_core import errors, hooks
    assert ("log_failures", "*") in hooks.registered()["post"]
    with caplog.at_level(logging.WARNING, logger="nova.tools"):
        errors.log_failures("file_processor", {}, "Error: 503 service unavailable", {})
        errors.log_failures("web_search", {}, "Here are the results you asked for", {})
    lines = [r.getMessage() for r in caplog.records if r.name == "nova.tools"]
    assert lines == ["[TOOL] file_processor failed (transient): Error: 503 service unavailable"]


# ── finding the window she means ─────────────────────────────────────────────
@pytest.mark.skipif(sys.platform != "win32", reason="window matching is Windows-only")
def test_a_guessed_or_url_shaped_title_still_finds_the_window(monkeypatch):
    from actions import app_control as ac
    windows = [(111, "Founder Profiles | Vester Accelerator - Google Chrome"),
               (222, "Untitled - Notepad"), (333, "NOVA")]
    monkeypatch.setattr(ac, "_enum_windows", lambda: windows)
    assert ac._find_handle("Founder Profiles - Google Chrome", timeout=0)[0] == 111
    assert ac._find_handle("vester accelerator application", timeout=0)[0] == 111
    assert ac._find_handle("notepad", timeout=0)[0] == 222
    assert ac._find_handle("Spotify", timeout=0) is None


@pytest.mark.skipif(sys.platform != "win32", reason="window matching is Windows-only")
def test_not_found_lists_the_real_windows(monkeypatch):
    from actions import app_control as ac
    monkeypatch.setattr(ac, "_enum_windows", lambda: [(1, "Founder Profiles | Vester - Google Chrome"),
                                                       (2, "Program Manager")])
    msg = ac._not_found("accelerator.vester.ai/apply/start")
    assert "Founder Profiles | Vester - Google Chrome" in msg and "Program Manager" not in msg
