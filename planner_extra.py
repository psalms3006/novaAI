"""
planner_extra.py
═════════════════
NOVAPlanner + _execute_planner extracted from nova.py (Phase 2). Zero
behavior change. _planner is owned by nova.py (reassigned in main() and
read from offline/UI code), so accessed via nova_state._planner, not aliased.
"""
from __future__ import annotations
import json, re, threading, time
from datetime import datetime, timedelta

from typing import List, Dict, Any, Optional, Callable

import nova_state
import nova as _nova
log = _nova.log
PLANNER_FILE = _nova.PLANNER_FILE

class NOVAPlanner:
    def __init__(self) -> None:
        self._tasks: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._checker: Optional[threading.Thread] = None
        self._speak_fn: Optional[Callable[[str], None]] = None
        self._load()
        self._start_checker()

    def set_speak(self, fn: Callable[[str], None]) -> None:
        self._speak_fn = fn

    def _load(self) -> None:
        if PLANNER_FILE.exists():
            try:
                self._tasks = json.loads(PLANNER_FILE.read_text(encoding="utf-8"))
            except Exception:
                self._tasks = []

    def _save(self) -> None:
        try:
            PLANNER_FILE.write_text(json.dumps(self._tasks, indent=2), encoding="utf-8")
        except Exception as e:
            log.error(f"Planner save failed: {e}")

    @staticmethod
    def _parse_time(time_str: str) -> Optional[datetime]:
        now = datetime.now()
        ts = time_str.strip().lower()
        m = re.match(r"in\s+(\d+)\s*(minute|min|hour|hr|day|second|sec)", ts)
        if m:
            amount = int(m.group(1))
            unit = m.group(2)
            if unit.startswith("sec"):
                return now + timedelta(seconds=amount)
            if unit.startswith("min"):
                return now + timedelta(minutes=amount)
            if unit.startswith(("hour", "hr")):
                return now + timedelta(hours=amount)
            if unit.startswith("day"):
                return now + timedelta(days=amount)
        if ts == "tomorrow":
            return (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        for pattern in [r"(?:at\s+)?(\d{1,2}):(\d{2})\s*(am|pm)?", r"(?:at\s+)?(\d{1,2})\s*(am|pm)"]:
            m2 = re.search(pattern, ts)
            if m2:
                groups = m2.groups()
                if len(groups) == 3:
                    hour_s, minute_s, ampm = groups
                    minute = int(minute_s) if minute_s else 0
                else:
                    hour_s, ampm = groups
                    minute = 0
                hour = int(hour_s)
                if ampm == "pm" and hour < 12:
                    hour += 12
                if ampm == "am" and hour == 12:
                    hour = 0
                target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if target < now:
                    target += timedelta(days=1)
                return target
        m3 = re.search(r"tomorrow\s+(?:at\s+)?(\d{1,2}):?(\d{2})?\s*(am|pm)?", ts)
        if m3:
            hour = int(m3.group(1))
            minute = int(m3.group(2)) if m3.group(2) else 0
            ampm = m3.group(3)
            if ampm == "pm" and hour < 12:
                hour += 12
            return (now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        return None

    def add(self, description: str, time_str: str) -> str:
        due = self._parse_time(time_str)
        if due is None:
            due = datetime.now() + timedelta(minutes=10)
            note = f" (couldn't parse '{time_str}', set to 10 min from now)"
        else:
            note = ""
        task_id = str(int(time.time()))
        with self._lock:
            self._tasks.append({"id": task_id, "description": description, "due": due.isoformat(), "done": False})
            self._save()
        return f"Reminder set: '{description}' at {due.strftime('%I:%M %p, %B %d')}{note}"

    def list_tasks(self) -> str:
        with self._lock:
            pending = [t for t in self._tasks if not t["done"]]
        if not pending:
            return "No pending reminders."
        lines = []
        for t in pending:
            due = datetime.fromisoformat(t["due"]).strftime("%I:%M %p, %b %d")
            lines.append(f"[{t['id'][-4:]}] {t['description']} — due {due}")
        return "\n".join(lines)

    def cancel(self, task_id: str) -> str:
        with self._lock:
            for t in self._tasks:
                if t["id"].endswith(task_id) or t["description"].lower() == task_id.lower():
                    t["done"] = True
                    self._save()
                    return f"Cancelled: {t['description']}"
        return f"Task not found: {task_id}"

    def clear_done(self) -> str:
        with self._lock:
            before = len(self._tasks)
            self._tasks = [t for t in self._tasks if not t["done"]]
            self._save()
        return f"Cleared {before - len(self._tasks)} completed tasks."

    def _start_checker(self) -> None:
        self._checker = threading.Thread(target=self._check_loop, daemon=True, name="NOVAPlanner")
        self._checker.start()

    def _check_loop(self) -> None:
        while True:
            time.sleep(30)
            now = datetime.now()
            with self._lock:
                due_now = []
                for t in self._tasks:
                    if t["done"]:
                        continue
                    if now >= datetime.fromisoformat(t["due"]):
                        t["done"] = True
                        due_now.append(t["description"])
                if due_now:
                    self._save()
            for desc in due_now:
                msg = f"Reminder: {desc}"
                print(f"\n⏰ {msg}")
                if self._speak_fn:
                    try:
                        self._speak_fn(msg)
                    except Exception:
                        pass


def _execute_planner(args: dict) -> str:

    if nova_state._planner is None:
        return "Planner not initialised yet."
    action = args.get("action", "list")
    if action == "add":
        return nova_state._planner.add(args.get("description", "reminder"), args.get("time", "in 10 minutes"))
    elif action == "list":
        return nova_state._planner.list_tasks()
    elif action == "cancel":
        return nova_state._planner.cancel(args.get("task_id", ""))
    elif action == "clear_done":
        return nova_state._planner.clear_done()
    return f"Unknown planner action: {action}"


# ══════════════════════════════════════════════════════════════════════════════
