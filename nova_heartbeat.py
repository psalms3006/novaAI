"""
nova_heartbeat.py — Tier 5 completion for Project NOVA
════════════════════════════════════════════════════════
Extends ProactiveAgent (nova_patch.py) with:
  - Held notices: fires offline → queued → shown on return
  - Quiet hours: configurable no-interrupt window
  - Dismissible inbox: notices user can clear
  - Persistent schedule: survives restarts via heartbeat_state.json
  - Kill switch: /pause and /resume commands via REPL

DROP-IN USAGE (add to nova.py main(), after ProactiveAgent init):
    from nova_heartbeat import Heartbeat
    _heartbeat = Heartbeat(speak_fn=nova.speak, meta=meta, planner=_planner)
    _heartbeat.start()

REPL commands (add to offline loop or Gemini Live text handler):
    if user_input.startswith("/"):
        from nova_heartbeat import handle_heartbeat_command
        result = handle_heartbeat_command(user_input, _heartbeat)
        if result: speak(result); continue
"""

from __future__ import annotations
import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional

# ── Config defaults (override via nova_config.toml) ─────────────────────────
_STATE_FILE      = Path("heartbeat_state.json")
_INBOX_FILE      = Path("heartbeat_inbox.json")
QUIET_HOUR_START = 22   # 10 PM — no non-urgent interrupts
QUIET_HOUR_END   = 7    # 7 AM
CHECK_INTERVAL   = 60   # seconds between ticks
MIN_GAP          = 300  # minimum seconds between unsolicited messages

# Load from nova_config.toml if present
try:
    import tomllib
    _cfg = tomllib.loads(Path("nova_config.toml").read_text())
    QUIET_HOUR_START = _cfg.get("heartbeat", {}).get("quiet_start", QUIET_HOUR_START)
    QUIET_HOUR_END   = _cfg.get("heartbeat", {}).get("quiet_end",   QUIET_HOUR_END)
    CHECK_INTERVAL   = _cfg.get("heartbeat", {}).get("interval",    CHECK_INTERVAL)
    MIN_GAP          = _cfg.get("heartbeat", {}).get("min_gap",     MIN_GAP)
except Exception:
    pass


# ── Notice ───────────────────────────────────────────────────────────────────

class Notice:
    def __init__(self, text: str, urgent: bool = False, source: str = "heartbeat"):
        self.id      = str(int(time.time() * 1000))
        self.text    = text
        self.urgent  = urgent
        self.source  = source
        self.ts      = datetime.now().isoformat()
        self.read    = False

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}

    @classmethod
    def from_dict(cls, d: dict) -> "Notice":
        n = cls.__new__(cls)
        n.__dict__.update(d)
        return n


# ── Inbox ────────────────────────────────────────────────────────────────────

class Inbox:
    """Thread-safe persistent notice queue."""

    def __init__(self):
        self._lock    = threading.Lock()
        self._notices: List[Notice] = []
        self._load()

    def _load(self):
        if _INBOX_FILE.exists():
            try:
                data = json.loads(_INBOX_FILE.read_text(encoding="utf-8"))
                self._notices = [Notice.from_dict(d) for d in data]
            except Exception:
                self._notices = []

    def _save(self):
        try:
            _INBOX_FILE.write_text(
                json.dumps([n.to_dict() for n in self._notices], indent=2),
                encoding="utf-8"
            )
        except Exception:
            pass

    def add(self, notice: Notice):
        with self._lock:
            self._notices.append(notice)
            self._save()

    def unread(self) -> List[Notice]:
        with self._lock:
            return [n for n in self._notices if not n.read]

    def dismiss_all(self):
        with self._lock:
            for n in self._notices:
                n.read = True
            self._save()

    def dismiss(self, notice_id: str) -> bool:
        with self._lock:
            for n in self._notices:
                if n.id == notice_id:
                    n.read = True
                    self._save()
                    return True
        return False

    def list_unread(self) -> str:
        items = self.unread()
        if not items:
            return "Inbox is empty."
        lines = [f"[{n.id[-4:]}] {'🔴' if n.urgent else '🔵'} {n.ts[:16]} — {n.text}" for n in items]
        return "\n".join(lines)


# ── Persistent schedule state ─────────────────────────────────────────────────

class HeartbeatState:
    """Persists when each check is next due so restarts don't reset timers."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict = {}
        self._load()

    def _load(self):
        if _STATE_FILE.exists():
            try:
                self._data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            except Exception:
                self._data = {}

    def _save(self):
        try:
            _STATE_FILE.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def get_next(self, key: str) -> Optional[datetime]:
        val = self._data.get(key)
        if val:
            try:
                return datetime.fromisoformat(val)
            except Exception:
                pass
        return None

    def set_next(self, key: str, dt: datetime):
        with self._lock:
            self._data[key] = dt.isoformat()
            self._save()

    def is_due(self, key: str) -> bool:
        nxt = self.get_next(key)
        if nxt is None:
            return True
        return datetime.now() >= nxt

    def schedule(self, key: str, interval_secs: int):
        self.set_next(key, datetime.now() + timedelta(seconds=interval_secs))


# ── Heartbeat ─────────────────────────────────────────────────────────────────

class Heartbeat:
    """
    Full Tier 5 heartbeat.

    Checks every CHECK_INTERVAL seconds. During quiet hours only urgent
    notices break through. Everything else goes to the inbox and surfaces
    when the user is back (catch-up-on-return via show_missed()).
    """

    def __init__(
        self,
        speak_fn:  Callable[[str], None],
        meta:      dict,
        planner:   Optional[object] = None,
    ):
        self._speak   = speak_fn
        self._meta    = meta
        self._planner = planner
        self._inbox   = Inbox()
        self._state   = HeartbeatState()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._paused  = False
        self._last_spoke: float = 0.0
        self._warned_reminders: set = set()

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._loop, daemon=True, name="NOVAHeartbeat"
        )
        self._thread.start()

    def stop(self):
        self._running = False

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    @property
    def is_paused(self) -> bool:
        return self._paused

    def show_missed(self) -> Optional[str]:
        """Call when user returns. Returns catch-up summary or None."""
        unread = self._inbox.unread()
        if not unread:
            return None
        count  = len(unread)
        urgent = [n for n in unread if n.urgent]
        msg    = f"While you were away — {count} notice{'s' if count > 1 else ''}."
        if urgent:
            msg += f" {len(urgent)} urgent: {urgent[0].text}"
        return msg

    def update_speak(self, fn: Callable[[str], None]):
        self._speak = fn

    def update_meta(self, meta: dict):
        self._meta = meta

    @property
    def inbox(self) -> Inbox:
        return self._inbox

    # ── Internal loop ─────────────────────────────────────────────────────────

    def _loop(self):
        time.sleep(15)  # give NOVA time to boot
        while self._running:
            try:
                if not self._paused:
                    self._tick()
            except Exception:
                pass
            time.sleep(CHECK_INTERVAL)

    def _in_quiet_hours(self) -> bool:
        h = datetime.now().hour
        if QUIET_HOUR_START > QUIET_HOUR_END:  # e.g. 22 → 7
            return h >= QUIET_HOUR_START or h < QUIET_HOUR_END
        return QUIET_HOUR_START <= h < QUIET_HOUR_END

    def _can_interrupt(self, urgent: bool = False) -> bool:
        if urgent:
            return True  # urgent always breaks through
        if self._in_quiet_hours():
            return False
        return (time.time() - self._last_spoke) >= MIN_GAP

    def _surface(self, text: str, urgent: bool = False):
        """Surface a notice — interrupt if allowed, else queue it."""
        n = Notice(text=text, urgent=urgent)
        if self._can_interrupt(urgent):
            self._last_spoke = time.time()
            try:
                self._speak(text)
            except Exception:
                pass
            n.read = True  # mark as delivered
        self._inbox.add(n)

    def _tick(self):
        now = datetime.now()

        # ── 1. Upcoming reminder warning (5 min ahead) ───────────────────────
        if self._planner:
            try:
                warn_window = now + timedelta(minutes=5)
                for task in getattr(self._planner, "_tasks", []):
                    if task.get("done"):
                        continue
                    tid = task.get("id", "")
                    if tid in self._warned_reminders:
                        continue
                    due_str = task.get("due", "")
                    if not due_str:
                        continue
                    due = datetime.fromisoformat(due_str)
                    if now <= due <= warn_window:
                        self._warned_reminders.add(tid)
                        self._surface(
                            f"Heads-up — your reminder '{task['description']}' "
                            f"is due in about 5 minutes.",
                            urgent=True,
                        )
                        return
            except Exception:
                pass

        # ── 2. Morning brief (once per day, 7–9 AM) ─────────────────────────
        if 7 <= now.hour <= 9 and self._state.is_due("morning_brief"):
            name     = self._meta.get("user_name", "")
            projects = self._get_projects()
            if projects:
                self._surface(
                    f"Good morning{', ' + name if name else ''}. "
                    f"You were last working on: {projects[0]}. "
                    f"Ready to pick up where you left off?"
                )
            self._state.schedule("morning_brief", 20 * 3600)  # once per ~day
            return

        # ── 3. End-of-day wrap (17:00, once per day) ─────────────────────────
        if now.hour == 17 and now.minute < 2 and self._state.is_due("eod_wrap"):
            name = self._meta.get("user_name", "")
            self._surface(
                f"It's 5 PM{', ' + name if name else ''}. "
                f"Ready to wrap up? I can summarise what we got done today."
            )
            self._state.schedule("eod_wrap", 20 * 3600)
            return

    def _get_projects(self) -> list:
        try:
            import nova as _nova
            texts = getattr(_nova, "_memory_texts", [])
            return [
                t for t in texts
                if any(kw in t.lower() for kw in ["project", "working on", "building", "nova", "task"])
            ][:2]
        except Exception:
            return []


# ── REPL command handler ──────────────────────────────────────────────────────

def handle_heartbeat_command(cmd: str, hb: Heartbeat) -> Optional[str]:
    """
    Handle /inbox, /dismiss, /pause, /resume, /heartbeat, and /goal commands.
    Returns reply string or None if not a heartbeat/goal command.
    """
    cmd = cmd.strip()
    if cmd == "/inbox":
        return hb.inbox.list_unread()
    if cmd == "/dismiss":
        hb.inbox.dismiss_all()
        return "Inbox cleared."
    if cmd.startswith("/dismiss "):
        nid = cmd.split(maxsplit=1)[1]
        return "Dismissed." if hb.inbox.dismiss(nid) else f"Notice {nid} not found."
    if cmd == "/pause":
        hb.pause()
        return "Proactive heartbeat paused. Say /resume to restart."
    if cmd == "/resume":
        hb.resume()
        return "Heartbeat resumed."
    if cmd == "/heartbeat":
        return (
            f"Heartbeat {'PAUSED' if hb.is_paused else 'ACTIVE'} | "
            f"Quiet hours: {QUIET_HOUR_START}:00–{QUIET_HOUR_END}:00 | "
            f"Unread notices: {len(hb.inbox.unread())}"
        )
    if cmd in ("/goal", "/goals"):
        try:
            from core.goal_engine import GoalEngine
            engine = GoalEngine()
            goals = engine.recover_unfinished()
            if not goals:
                return "No active goals."
            lines = [f"• {g.goal_id}: {g.mission} [{g.status}]" for g in goals]
            return "
".join(lines)
        except Exception as e:
            return f"Goal lookup failed: {e}"
    if cmd.startswith("/goal create "):
        mission = cmd.split(maxsplit=2)[2]
        try:
            from core.goal_engine import GoalEngine
            engine = GoalEngine()
            goal = engine.create_goal(mission)
            return f"Goal created: {goal.goal_id}"
        except Exception as e:
            return f"Goal creation failed: {e}"
    return None
