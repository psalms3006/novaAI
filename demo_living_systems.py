#!/usr/bin/env python3
"""
NOVA living systems self-demo.

Shows, end-to-end and without touching your real stores:
  1. Living memory  -> structured recall, recall-by-query, stats
  2. AIOS task manager -> a multi-step task that runs, verifies,
     writes a real artifact, notifies, and persists across sessions

Run:  python demo_living_systems.py
"""

import os
import sys
import time
import uuid
import tempfile

from living_memory import LivingMemory, init_living_memory
from task_manager import TaskManager


class DemoExec:
    """Mimics nova._execute_tool_sync for the demo (standalone, no nova import)."""

    def __init__(self, workdir: str):
        self.workdir = workdir

    def __call__(self, tool, args, meta):
        if tool == "web_search":
            query = args.get("query", "nova")
            return f"Results: found 3 sources about '{query}'."
        if tool == "write_file":
            name = args.get("file_path") or "report.txt"
            path = name if os.path.isabs(name) else os.path.join(self.workdir, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("NOVA living systems report.\n- memory: structured recall\n- tasks: verified background jobs\n")
            return f"Created: {path}"
        if tool == "boom":
            raise RuntimeError("boom step exploded")
        return "Done."


def demo_verify(text: str) -> str:
    low = (text or "").lower()
    if any(w in low for w in ("error:", "exception", "failed")):
        return "FAILURE"
    if any(w in low for w in ("found ", "created:", "results:", "saved:")):
        return "CONFIRMED_SUCCESS"
    return "UNKNOWN"


def main() -> int:
    print("=" * 62)
    print("  NOVA living systems demo (temporary stores only)")
    print("=" * 62)

    base = tempfile.mkdtemp(prefix="nova_demo_")

    # ── 1. living memory ──────────────────────────────────────────────────────
    mem_path = os.path.join(base, "living_memory.json")
    # init_living_memory sets the module singleton so the task manager's
    # outcome-remembering hook (`_notify_done`) records task results into memory.
    mem = init_living_memory(path=mem_path, mirror=False, start_maintenance_loop=False)
    print("\n[1] Living memory")
    mem.remember("User studies Mechatronics at FUTO.", source="explicit", importance=0.9)
    mem.remember("User prefers dark mode for all interfaces.", source="explicit", importance=0.7)
    mem.remember("NOVA building project uses Python and SQLite.", source="explicit",
                 project="nova-building", importance=0.8)
    mem.exec_command("update", "NOVA building project uses Python 3.11 and SQLite.", project="nova-building")

    print("  stored + 1 correction versioned")
    hits = mem.search("what does the user study", top_k=3)
    print("  recall('what does the user study'):")
    for r in hits:
        print(f"    - [{r['text']}]")
    print(f"  stats: {mem.stats()}")

    # ── 2. AIOS task manager ──────────────────────────────────────────────────
    task_path = os.path.join(base, "aios_tasks.json")
    workdir = os.path.join(base, "work")
    os.makedirs(workdir, exist_ok=True)

    tm = TaskManager(path=task_path, tool_executor=DemoExec(workdir), verify_fn=demo_verify)
    notifications = []
    tm.set_notify(notifications.append)
    tm.start()

    print("\n[2] AIOS task manager")
    steps = [
        {"tool": "web_search", "args": {"query": "background agents 2026"}},
        {"tool": "write_file", "args": {"file_path": os.path.join(workdir, "report.txt")}},
    ]
    t = tm.submit("Demo: research + write report", steps)
    print(f"  submitted -> {t.id} '{t.title}' ({t.status})")
    print("  status: " + tm.progress_text().replace("\n", " | "))

    deadline = time.time() + 15
    while t.status in ("PLANNED", "QUEUED", "RUNNING", "VERIFYING") and time.time() < deadline:
        time.sleep(0.05)
    print(f"  final: {t.status}  (steps={[s.verification for s in t.steps]})")
    print(f"  notification: {notifications[-1] if notifications else 'none'}")
    if t.artifacts:
        print(f"  validated artifact: {t.artifacts[0]['path']} "
              f"(exists={t.artifacts[0]['exists']}, size={t.artifacts[0]['size']})")

    # persistence across sessions
    tm2 = TaskManager(path=task_path, tool_executor=DemoExec(workdir), verify_fn=demo_verify)
    again = tm2.get(t.id)
    print(f"  persists across sessions: {again is not None and again.status == t.status}")

    # task outcome became recallable memory (via the task manager's memory hook)
    outcomes = [r["text"] for r in mem.all() if "Task '" in r["text"]]
    print(f"  task outcome remembered: {bool(outcomes)} -> {outcomes[0] if outcomes else 'none'}")
    print(f"\n[done] demo stores under: {base}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
