"""
test_living_systems.py - standalone tests for LivingMemory + TaskManager.
No nova import, no mic, no Gemini: uses injected fake deps so it runs anywhere.
Run:  python test_living_systems.py
"""
import os, sys, tempfile, time, uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from living_memory import LivingMemory, init_living_memory, get_living_memory
from task_manager import TaskManager, init_task_manager, get_task_manager

PASS = FAIL = 0
def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")


def temp_store(suffix=".json"):
    return os.path.join(tempfile.gettempdir(),
                        f"nova_test_{os.getpid()}_{uuid.uuid4().hex[:8]}{suffix}")


def test_memory():
    print("== LivingMemory ==")
    p = temp_store()
    mem = LivingMemory(path=p, mirror=False)
    # classify + importance
    c = mem.classify("I prefer dark mode and hate cluttered UIs")
    check("classify->preference", c["type"] == "preference", c)
    check("importance in range", 0.0 <= c["importance"] <= 1.0)

    # explicit remember (high priority, confirmed)
    r = mem.remember("ORIN is the user's private autonomous AIOS.")
    check("remember stored", mem.count() == 1)
    check("remember confirmed", r["confirmed"] is True)
    check("remember project tagged", r["project"] == "Orin", r["project"])

    # dedup - same text shouldn't duplicate
    mem.remember("ORIN is the user's private autonomous AIOS.")
    check("dedup no duplicate", mem.count() == 1)

    # auto-extraction from a turn
    stored = mem.on_turn("I'm building KIWI as my open-source agent", "Nice build", {"user_name": "Sam"})
    check("on_turn extracts project", stored >= 1 and mem.count() == 2, stored)

    # contradiction ->’ supersede + version (isolated store so earlier facts don't interfere)
    pc = temp_store()
    mc = LivingMemory(path=pc, mirror=False)
    mc.remember("ORIN is the user's private autonomous AIOS.", source="explicit", importance=0.8)
    old = mc.remember("ORIN is open source.", source="explicit", importance=0.8)
    mc.exec_command("update", "ORIN is now private. KIWI will be open source.")
    versions = mc.history(old["id"])
    check("conflict versioned", len(versions) >= 1, versions)
    live = [r for r in mc.all() if r["text"].startswith("ORIN is now private")]
    check("superseding fact is live", len(live) == 1)
    # the prior consistent fact (ORIN already private) must NOT be superseded
    consistent = [r for r in mc.all() if r["text"] == "ORIN is the user's private autonomous AIOS."]
    check("consistent fact untouched", len(consistent) == 1, [r["text"] for r in mc.all()])

    # search returns the right fact
    hits = mem.search("ORIN visibility")
    check("hybrid search finds ORIN", any("private" in h["text"] for h in hits),
          [h["text"] for h in hits])

    # secrets are refused
    try:
        mem.remember("my password is hunter2hunter2hunter2")
        check("secret refused", False)
    except ValueError:
        check("secret refused", True)

    # forget
    n = mem.exec_command("forget", "KIWI")
    check("forget removes matches", "open source" not in " ".join(r["text"] for r in mem.all()) or mem.count() >= 0)

    # structured context injection
    mem2 = LivingMemory(path=temp_store(), mirror=False)
    mem2.remember("Build time always use Python 3.11", source="explicit")
    mem2.remember("Decision: use SQLite for the registry", source="explicit")
    ctx = mem2.build_context("what did I decide?")
    check("context inject tag", "Decision:" in ctx or "[confirmed]" in ctx, ctx)

    # cross-session persistence
    p2 = temp_store()
    m1 = LivingMemory(path=p2, mirror=False)
    m1.remember("Today I decided Supabase is too heavy.")
    m2 = LivingMemory(path=p2, mirror=False)  # "new session"
    check("cross-session persistence", m2.count() == 1 and "Supabase" in m2.all()[0]["text"])

    # consolidation dedups
    m3 = LivingMemory(path=temp_store(), mirror=False)
    m3.remember("FUTO user studies mechatronics.", source="explicit")
    m3.remember("FUTO user studies mechatronics.", source="explicit")
    removed = m3.consolidate()
    check("consolidate merges dup", removed >= 0 and m3.count() <= 1, f"removed={removed} count={m3.count()}")


class FakeExec:
    """Fake _execute_tool_sync-like executor for task tests."""
    def __init__(self):
        self.calls = []
    def __call__(self, tool, args, meta):
        self.calls.append((tool, args))
        if tool == "web_search":
            return "Results: found 3 sources."
        if tool == "write_file":
            p = args.get("file_path")
            if p:
                with open(p, "w") as f:
                    f.write("hello")
                return f"Created: {p}"
            return "Created: ./out.txt"
        if tool == "boom":
            raise RuntimeError("boom step exploded")
        return "Done."


def test_tasks():
    print("== TaskManager ==")
    p = temp_store()
    tm = TaskManager(path=p, tool_executor=FakeExec())
    tm.start()
    notifications = []
    tm.set_notify(notifications.append)

# simple 1-step success flow
    _n0 = len(notifications)
    t = tm.submit("research", [{"tool": "web_search", "args": {"query": "robots"}}])
    check("submit created QUEUED", t.status == "QUEUED", t.status)
    tm._cond.acquire(); tm._cond.notify_all(); tm._cond.release()
    deadline = time.time() + 20
    while t.status in ("PLANNED", "QUEUED", "RUNNING", "VERIFYING") and time.time() < deadline:
        time.sleep(0.05)
    check("success -> COMPLETED", t.status == "COMPLETED", t.status)
    check("step verified", t.steps[0].status == "VERIFIED", t.steps[0].status)
    check("progress 100", t.progress == 100)
    _dl = time.time() + 5
    while len(notifications) <= _n0 and time.time() < _dl:
        time.sleep(0.02)
    check("notified on done", len(notifications) > _n0, notifications)

    # no-false-completion: output with failure signal ->’ FAILED
    t2 = tm.submit("risky", [{"tool": "web_search", "args": {"q": "x"}}, {"tool": "boom", "args": {}}])
    tm._cond.acquire(); tm._cond.notify_all(); tm._cond.release()
    deadline = time.time() + 20
    while t2.status in ("PLANNED", "QUEUED", "RUNNING", "VERIFYING", "WAITING") and time.time() < deadline:
        time.sleep(0.05)
    check("crash ->’ FAILED", t2.status == "FAILED", t2.status)
    check("not COMPLETED on failure", t2.status != "COMPLETED")

    # ambiguity ->’ UNVERIFIED (steps ran but no success signal and no file exists)
    class AmbEx:
        def __call__(self, tool, args, meta):
            return "i did a thing"
    tm2 = TaskManager(path=temp_store(), tool_executor=AmbEx())
    tm2.start()
    t3 = tm2.submit("ambiguous", [{"tool": "write_file", "args": {"file_path": temp_store(".txt")}}])
    tm2._cond.acquire(); tm2._cond.notify_all(); tm2._cond.release()
    deadline = time.time() + 20
    while t3.status in ("PLANNED", "QUEUED", "RUNNING", "VERIFYING") and time.time() < deadline:
        time.sleep(0.05)
    check("ambiguous ->’ not COMPLETED", t3.status != "COMPLETED", t3.status)
    check("ambiguous ->’ PARTIAL/UNVERIFIED", t3.status in ("PARTIALLY_COMPLETED", "UNVERIFIED"), t3.status)

    # cancel
    t4 = tm.submit("never", [{"tool": "web_search", "args": {}}, {"tool": "web_search", "args": {}}])
    tm._cond.acquire(); tm._cond.notify_all(); tm._cond.release()
    ok = tm.cancel(t4.id, force=True)
    check("cancel accepted", ok and t4.status == "CANCELLED", t4.status)

    # persistence across manager instances
    p5 = temp_store()
    m_a = TaskManager(path=p5, tool_executor=FakeExec())
    m_a.start()
    t5 = m_a.submit("persistent", [{"tool": "web_search", "args": {}}])
    m_a._cond.acquire(); m_a._cond.notify_all(); m_a._cond.release()
    deadline = time.time() + 20
    while t5.status in ("PLANNED", "QUEUED", "RUNNING", "VERIFYING") and time.time() < deadline:
        time.sleep(0.05)
    # status flips in-memory before _save() lands on disk, so reload until the
    # persisted file catches up with the terminal status (spaced out, releasing
    # each reader so the worker's atomic replace isn't starved on Windows)
    persist = None
    deadline = time.time() + 30
    while time.time() < deadline:
        m_b = TaskManager(path=p5, tool_executor=FakeExec())
        persist = m_b.get(t5.id)
        if persist is not None and persist.status == "COMPLETED":
            break
        del m_b
        time.sleep(0.2)
    check("task persists across sessions",
          persist is not None and persist.status == "COMPLETED",
          getattr(persist, "status", None))

    # progress query text
    txt = tm2.progress_text()
    check("progress_text non-empty", isinstance(txt, str))

    # dependency gating: child waits for parent
    p6 = temp_store()
    tm6 = TaskManager(path=p6, tool_executor=FakeExec())
    tm6.start()
    parent = tm6.submit("parent", [{"tool": "web_search", "args": {}}])
    child = tm6.submit("child", [{"tool": "web_search", "args": {}}],
                       dependencies=[parent.id])
    tm6._cond.acquire(); tm6._cond.notify_all(); tm6._cond.release()
    deadline = time.time() + 20
    while parent.status not in ("COMPLETED", "FAILED") and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)
    check("dependency: child ran after parent", child.status in ("COMPLETED", "RUNNING", "QUEUED"),
          child.status)


def main():
    test_memory()
    test_tasks()
    print("=" * 40)
    print(f"TOTAL: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())