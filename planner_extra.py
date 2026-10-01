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
        # "tomorrow at 8pm" before the bare time patterns: they matched "8pm"
        # first and set the reminder for today.
        m3 = re.search(r"tomorrow\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", ts)
        if m3:
            hour = int(m3.group(1))
            minute = int(m3.group(2)) if m3.group(2) else 0
            ampm = m3.group(3)
            if ampm == "pm" and hour < 12:
                hour += 12
            if ampm == "am" and hour == 12:
                hour = 0
            return (now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
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


#: Workflow kinds NOVA registers a runner for (nova._start_ambient_intelligence).
REMINDER_KIND = "reminder"
GOAL_KIND = "nova_goal"


def parse_every(text: str) -> tuple:
    """'daily' -> (86400, False); 'weekdays' -> (86400, True); 'every 2 hours'
    -> (7200, False). (None, False) if it is not a repetition."""
    t = (text or "").strip().lower()
    if not t:
        return None, False
    if re.search(r"week ?days|every weekday|monday to friday|mon-fri", t):
        return 86400.0, True
    named = {"hourly": 3600, "every hour": 3600, "daily": 86400, "every day": 86400,
             "each day": 86400, "every morning": 86400, "every evening": 86400, "nightly": 86400,
             "weekly": 604800, "every week": 604800}
    for k, v in named.items():
        if k in t:
            return float(v), False
    m = re.search(r"(\d+)\s*(minute|min|hour|hr|day|week)s?", t)
    if m:
        unit = {"min": 60, "minute": 60, "hour": 3600, "hr": 3600, "day": 86400, "week": 604800}[m.group(2)]
        return float(int(m.group(1)) * unit), False
    return None, False


def _scheduler():
    from nova_scheduler import get_scheduler
    return nova_state._scheduler or get_scheduler()


def _add_workflow(description: str, time_str: str, every: str, run: str) -> str:
    """A repeating reminder, or a task NOVA performs on a schedule."""
    from nova_scheduler import ApprovalMode, Workflow
    seconds, weekdays = parse_every(every)
    if seconds is None:
        return (f"I couldn't tell how often from {every!r}. Say e.g. 'daily', 'weekdays', "
                f"'hourly', 'weekly' or 'every 30 minutes'.")
    first = NOVAPlanner._parse_time(time_str) if time_str else None
    starts = first.timestamp() if first else time.time() + seconds
    goal = (run or "").strip()
    w = Workflow(title=(description or goal or "Scheduled")[:120],
                 kind=GOAL_KIND if goal else REMINDER_KIND,
                 every_seconds=seconds, starts_at=starts,
                 approval=ApprovalMode.FULL_AUTO,
                 params={"text": description or goal, "goal": goal, "weekdays_only": weekdays})
    _scheduler().add(w)
    when = datetime.fromtimestamp(w.next_due).strftime("%a %d %b, %I:%M %p")
    what = f"I'll do this each time: {goal}" if goal else f"I'll remind you: {w.params['text']}"
    cadence = "every weekday" if weekdays else every
    return f"Scheduled ({w.id}): {what} -- {cadence}, first on {when}."


def _list_all() -> str:
    lines = [nova_state._planner.list_tasks()] if nova_state._planner else []
    try:
        ws = [w for w in _scheduler().workflows()
              if w.kind in (REMINDER_KIND, GOAL_KIND) and w.status.value in ("active", "paused")]
    except Exception:
        ws = []
    for w in ws:
        when = datetime.fromtimestamp(w.next_due).strftime("%I:%M %p, %b %d")
        kind = "task" if w.kind == GOAL_KIND else "reminder"
        lines.append(f"[{w.id}] repeating {kind}: {w.title} -- next {when}, {w.status.value}")
    return "\n".join(l for l in lines if l) or "No pending reminders."


def _find_workflow(ref: str):
    ref = (ref or "").strip().lower()
    if not ref:
        return None
    for w in _scheduler().workflows():
        if w.kind in (REMINDER_KIND, GOAL_KIND) and (
                w.id.lower() == ref or ref in w.title.lower()):
            return w
    return None


def _execute_planner(args: dict) -> str:

    if nova_state._planner is None:
        return "Planner not initialised yet."
    action = args.get("action", "list")
    if action == "add":
        every = str(args.get("every") or args.get("repeat") or "").strip()
        run = str(args.get("run") or "").strip()
        if every or run:
            return _add_workflow(str(args.get("description") or ""), str(args.get("time") or ""),
                                 every or "daily", run)
        return nova_state._planner.add(args.get("description", "reminder"), args.get("time", "in 10 minutes"))
    elif action == "list":
        return _list_all()
    elif action in ("cancel", "pause", "resume"):
        ref = str(args.get("task_id") or args.get("description") or "")
        w = _find_workflow(ref)
        if w is not None:
            ok = getattr(_scheduler(), action)(w.id)
            return f"{action.capitalize()}{'d' if action != 'cancel' else 'led'}: {w.title}" if ok \
                else f"Couldn't {action} {w.title}."
        if action == "cancel":
            return nova_state._planner.cancel(ref)
        return f"No repeating reminder or task matches {ref!r}."
    elif action == "clear_done":
        return nova_state._planner.clear_done()
    return f"Unknown planner action: {action}"


# ══════════════════════════════════════════════════════════════════════════════
