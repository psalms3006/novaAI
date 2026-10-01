"""
task_manager.py
════════════════
AIOS Concurrent Task Manager — background multi-step tasks that NEVER block the
voice/conversation loop, with verified-completion semantics.

NOVA orchestrates; this module is where the work actually happens.

  • A pool of worker threads (3 by default), so the Live asyncio loop and the
    offline loop stay responsive and independent tasks run side by side.
  • Task lifecycle: QUEUED → PLANNING (when given a goal, not steps) →
    RUNNING (→ WAITING while a shared resource such as the desktop is busy)
    → REVIEWING → COMPLETED | FAILED | CANCELLED | PARTIALLY_COMPLETED |
    UNVERIFIED. PAUSED holds a queued task back.
  • Every step belongs to an agent (research, browser, computer, creative,
    ...; see agent_activity). The agent is shown as running while its step
    runs, and the hand-offs between agents are recorded as structured
    messages on the task.
  • Step output flows forward: a document step with no body of its own is
    written from what the research steps before it found.
  • A reviewer checks the finished work and sends concrete problems back to
    the agent responsible, which tries again -- a bounded number of times.
  • NO FALSE COMPLETION: a task is only COMPLETED after every step is verified
    and the review passed. Otherwise it says what is missing.
  • Progress is measured -- steps done out of steps planned -- never guessed.
  • Every change is an event (task.*, agent.*, artifact.*, review.*) for the
    window, and part of the task's own execution history.
  • Persistence to nova_tasks_aios.json (atomic write) → survives restart.
  • Notify via an injected callable at the end of a task only -- internal
    events are for the screen, never narrated aloud.

Import-safe: does NOT import nova at module top; the tool-executor is injected,
so it can be unit-tested standalone.
"""
from __future__ import annotations
import json, logging, os, re, threading, time, uuid
from dataclasses import dataclass, field, fields
from typing import Any, Callable, Dict, List, Optional

import agent_activity
import nova_personality as _persona
import nova_paths

log = logging.getLogger("nova.task")

__all__ = ["TaskManager", "TaskStep", "get_task_manager", "init_task_manager"]

TASK_STATUSES = ["PLANNED", "QUEUED", "PLANNING", "RUNNING", "WAITING", "BLOCKED",
                 "REVIEWING", "VERIFYING", "PAUSED", "COMPLETED", "FAILED",
                 "CANCELLED", "PARTIALLY_COMPLETED", "UNVERIFIED"]
STEP_STATUSES = ["QUEUED", "RUNNING", "WAITING", "VERIFIED", "UNVERIFIED",
                 "FAILED", "SKIPPED"]
#: A task in one of these is finished and will not change again.
TERMINAL = ("COMPLETED", "FAILED", "CANCELLED", "PARTIALLY_COMPLETED", "UNVERIFIED")
#: A task in one of these is being worked on right now.
ACTIVE = ("PLANNING", "RUNNING", "WAITING", "REVIEWING", "VERIFYING")

TASKS_FILENAME = "nova_tasks_aios.json"

#: How many times the reviewer may send work back before the task stops
#: and says what is still wrong. Unbounded review loops burn quota forever.
MAX_REVIEW_ROUNDS = 2
#: Bounded logs on a task, so a long-lived one cannot grow without limit.
MAX_HISTORY = 200
MAX_MESSAGES = 100


def default_tasks_path() -> str:
    """Resolved per call, not at import.

    This was anchored to the module's own directory, which for a frozen
    install is the install directory — not writable by a standard user, and
    `_save()` swallows every exception, so tasks would have failed to persist
    in silence. In development it is the repository, which is how one user's
    tasks came to be a tracked file.
    """
    return str(nova_paths.data_file(TASKS_FILENAME))


def _extract_paths(text: str) -> List[str]:
    """Best-effort extraction of file-ish paths from a string."""
    out = []
    for m in re.finditer(r"(?:[A-Za-z]:\\|/|\./|~/)[^\s\"']{3,180}", text):
        p = m.group(0).rstrip(".,;:)]}")
        # Not "//host/..." out of a URL: on Windows that is a network share,
        # and checking whether it exists stalls for as long as SMB takes to
        # give up -- a research task sat in RUNNING on its own findings.
        if m.start() and text[m.start() - 1] in ":/\\":
            continue
        if p.startswith(("//", "\\\\")):
            continue
        if p and not p.endswith((".com", ".net", ".org", ".html", ".py:")):
            out.append(p)
    return out[:10]


#: How the writing tools report a file they wrote: "Saved <path> (N bytes)."
#: (generate_document) and "Saved to <path> (N bytes)" (file_controller).
#: Both re-read the file before saying so.
_SAVED = re.compile(r"^saved(?: to)?:?\s+(?P<path>.+?)\s+\((?P<size>\d+) bytes\)", re.I)

#: Tools that write their "content" argument to a file.
WRITER_TOOLS = ("generate_document", "file_controller")
RESEARCH_TOOLS = ("web_search",)
_WRITE_ACTIONS = ("write", "create", "save", "append")


def _placeholder(value: Any) -> bool:
    """A body the model left for someone else to fill ("...", "{{results}}")."""
    if not isinstance(value, str):
        return value is None
    s = value.strip()
    return (not s or s in ("TBD", "tbd") or "{{" in s
            or bool(re.fullmatch(r"[.…\s]+", s)))


def _writes_file(step: "TaskStep") -> bool:
    if step.tool == "generate_document":
        return True
    return (step.tool == "file_controller"
            and str(step.args.get("action") or "").lower() in _WRITE_ACTIONS)


def describe_step(tool: str, args: Dict[str, Any]) -> str:
    """What a step does, in words for the task panel."""
    a = args or {}
    if tool == "web_search":
        return f"Search: {a.get('query') or a.get('q') or 'the web'}"
    if tool == "generate_document":
        fmt = a.get("format") or "pdf"
        return f"Write '{a.get('title') or 'document'}' ({fmt})"
    if tool == "browser_control":
        return f"Open {a.get('url') or a.get('query') or 'the browser'}"
    if tool == "file_controller":
        return f"{str(a.get('action') or 'file').capitalize()} {a.get('path') or a.get('name') or ''}".strip()
    if tool in ("open_app", "close_app"):
        return f"{'Open' if tool == 'open_app' else 'Close'} {a.get('app_name') or a.get('name') or 'an app'}"
    if tool == "learn_resource":
        return f"Read {a.get('url') or a.get('path') or 'a source'}"
    return tool.replace("_", " ")


@dataclass
class TaskStep:
    tool: str
    args: Dict[str, Any]
    status: str = "QUEUED"
    result: str = ""
    verification: str = "UNKNOWN"      # CONFIRMED_SUCCESS / LIKELY_SUCCESS / FAILURE / UNKNOWN
    verified: bool = False
    retries: int = 0
    started: float = 0.0
    finished: float = 0.0
    error: str = ""
    agent: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        self.args = dict(self.args or {})
        self.agent = self.agent or agent_activity.agent_for_tool(self.tool)
        self.description = self.description or describe_step(self.tool, self.args)

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TaskStep":
        return cls(**{k: d.get(k, v) for k, v in cls.__dataclass_fields__.items()
                      if k in d})


@dataclass
class Task:
    title: str
    id: str = field(default_factory=lambda: f"T-{uuid.uuid4().hex[:8]}")
    status: str = "PLANNED"
    steps: List[TaskStep] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    priority: str = "NORMAL"
    #: Seconds NOVA told the user this would take ("about ten minutes").
    #: Shown as what she said, never turned into a progress figure: an
    #: estimate is not a measurement.
    estimated_duration_s: float = 0.0
    progress: int = 0
    current_step: int = -1
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0
    errors: List[str] = field(default_factory=list)
    reason_for_stop: str = ""
    notify_done: bool = True
    #: What the person asked for, as the goal handed to the planner.
    user_request: str = ""
    parent_task_id: str = ""
    #: True until the planner has turned user_request into steps.
    needs_plan: bool = False
    #: What is happening now, in words ("Search: ...", "Reviewing the work").
    phase: str = ""
    #: Structured hand-offs between agents: {id, ts, from, to, kind, text, step}.
    agent_messages: List[Dict[str, Any]] = field(default_factory=list)
    #: Everything that happened, in order: {ts, event, detail}.
    history: List[Dict[str, Any]] = field(default_factory=list)
    #: One ReviewResult per review round: {round, passed, issues, checked, ts}.
    reviews: List[Dict[str, Any]] = field(default_factory=list)

    # ── derived ────────────────────────────────────────────────────────────
    def steps_done(self) -> int:
        return sum(1 for s in self.steps if s.status in ("VERIFIED", "UNVERIFIED", "SKIPPED"))

    def agents(self) -> List[str]:
        out = ["orchestrator"] + [s.agent for s in self.steps]
        if self.reviews or self.status == "REVIEWING":
            out.append("reviewer")
        return list(dict.fromkeys(out))

    def elapsed_s(self) -> float:
        if not self.started:
            return 0.0
        return max(0.0, (self.finished or time.time()) - self.started)

    def to_dict(self) -> Dict[str, Any]:
        cur = self.steps[self.current_step] if 0 <= self.current_step < len(self.steps) else None
        nxt = next((s for s in self.steps if s.status == "QUEUED"), None)
        return {
            "title": self.title, "id": self.id, "status": self.status,
            "steps": [s.to_dict() for s in self.steps],
            "dependencies": list(self.dependencies),
            "artifacts": list(self.artifacts), "priority": self.priority,
            "progress": self.progress_percent(), "current_step": self.current_step,
            "created": self.created, "updated": self.updated,
            "started": self.started, "finished": self.finished,
            "errors": list(self.errors), "reason_for_stop": self.reason_for_stop,
            "notify_done": self.notify_done,
            "estimated_duration_s": self.estimated_duration_s,
            "seconds_remaining": self.seconds_remaining(),
            "user_request": self.user_request,
            "parent_task_id": self.parent_task_id,
            "needs_plan": self.needs_plan,
            "phase": self.phase,
            "agent_messages": list(self.agent_messages),
            "history": list(self.history),
            "reviews": list(self.reviews),
            # measured counters for the window
            "steps_done": self.steps_done(),
            "steps_total": len(self.steps),
            "elapsed_s": round(self.elapsed_s(), 1),
            "current": cur.description if cur is not None and self.status in ACTIVE else "",
            "next": nxt.description if nxt is not None and self.status in ACTIVE + ("QUEUED",) else "",
            "agents": self.agents(),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Task":
        steps = [TaskStep.from_dict(s) for s in d.get("steps", [])]
        t = cls(title=d.get("title", "Task"), steps=steps)
        # Only real dataclass fields: to_dict() also writes computed values
        # (seconds_remaining), and a hasattr() check would let those
        # overwrite the method of the same name with a plain value.
        settable = {f.name for f in fields(cls)} - {"steps"}
        for k, v in d.items():
            if k in settable:
                setattr(t, k, v)
        return t

    def progress_percent(self) -> int:
        """0-100: the share of planned steps that have finished.

        Measured, not estimated. It used to run off the clock against the
        estimate NOVA gave aloud, which made a bar that filled at the rate of
        a guess and sat at 95% for a task that had stalled. A task still being
        planned has no steps to count and reports 0; a COMPLETED one is 100,
        and nothing else is ever 100.
        """
        if self.status == "COMPLETED":
            return 100
        if not self.steps:
            return 0
        return max(0, min(99, int(100 * self.steps_done() / len(self.steps))))

    def seconds_remaining(self) -> Optional[float]:
        """Time left against the estimate NOVA gave, or None without one.

        This is her own stated figure counted down, not a prediction, and the
        window labels it so. None when there is nothing to count down."""
        if self.status not in ACTIVE or self.estimated_duration_s <= 0 or not self.started:
            return None
        return max(0.0, self.estimated_duration_s - (time.time() - self.started))

    def summary(self, detail: bool = False) -> str:
        done = sum(1 for s in self.steps if s.status in ("VERIFIED", "SKIPPED"))
        total = len(self.steps)
        lines = [f"{self.id} · {self.title} · {self.status}"
                 + (f" ({self.steps_done()}/{total} steps)" if total else "")]
        if self.phase and self.status in ACTIVE:
            lines.append(f"  Now: {self.phase}")
        if detail and self.steps:
            lines.append("  Steps:")
            for s in self.steps:
                lines.append(f"    [{s.status}] {s.agent}: {s.description[:70]}"
                             f"{' → ' + s.verification if s.result else ''}")
        elif self.steps:
            lines.append(f"  Step {self.current_step + 1}/{total} ({done} verified)")
        if self.reason_for_stop:
            lines.append(f"  Reason: {self.reason_for_stop}")
        return "\n".join(lines)


class TaskManager:
    """Background task executor running on a pool of daemon threads."""

    def __init__(self, path: Optional[str] = None,
                 tool_executor: Optional[Callable[[str, Dict, Dict], Any]] = None,
                 verify_fn: Optional[Callable[[str], str]] = None,
                 max_concurrent: int = 3) -> None:
        self.path = path or default_tasks_path()
        self._tool_executor = tool_executor      # fn(tool_name, args, meta) -> result str
        self._verify_fn = verify_fn              # fn(output_text) -> verification status
        self._notify: Optional[Callable[[str], None]] = None
        self._on_activity: Optional[Callable[[bool], None]] = None
        self._on_event: Optional[Callable[[Dict[str, Any]], None]] = None
        self._speak = None
        self._lock = threading.RLock()
        self._tasks: List[Task] = []
        # A worker pool, not one dedicated thread: independent tasks (no
        # shared dependency chain) can genuinely run at the same time. 3 by
        # default -- this runs on the same machine as everything else NOVA
        # does, including local model inference.
        self._max_concurrent = max(1, int(max_concurrent))
        self._threads: List[threading.Thread] = []
        self._stop = threading.Event()
        self._cond = threading.Condition(self._lock)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._load()

    # ── wiring ──────────────────────────────────────────────────────────────
    def set_notify(self, fn: Optional[Callable[[str], None]]) -> None:
        self._notify = fn

    def set_on_activity(self, fn: Optional[Callable[[bool], None]]) -> None:
        """Observe whether background work is in progress.

        Called with True when a task starts and False when the queue drains.
        """
        self._on_activity = fn

    def set_on_event(self, fn: Optional[Callable[[Dict[str, Any]], None]]) -> None:
        """Observe every task event (task.*, agent.message, artifact.*, review.*)."""
        self._on_event = fn

    def _signal_activity(self, busy: bool) -> None:
        """Never raises: this runs on the worker thread."""
        fn = self._on_activity
        if fn is None:
            return
        try:
            fn(busy)
        except Exception:
            log.debug("[TASK] activity observer failed", exc_info=True)

    def set_tool_executor(self, fn: Callable[[str, Dict, Dict], Any]) -> None:
        self._tool_executor = fn

    def set_verify(self, fn: Callable[[str], str]) -> None:
        self._verify_fn = fn

    def start(self) -> None:
        """Start the worker pool -- self._max_concurrent threads, each running
        the identical claim-a-task/run-it/loop-again cycle. The claim itself
        happens under self._cond, which is what makes several workers safe."""
        stopping = self._stop.is_set()
        if stopping:
            # Workers told to stop leave within a second (their wait). Let
            # them go before starting a new pool, or a start() just after a
            # stop() would find them "alive", do nothing, and then be left
            # with no workers at all once they exit.
            for t in list(self._threads):
                t.join(timeout=2.0)
        with self._lock:
            if self._threads and any(t.is_alive() for t in self._threads):
                return
            self._stop.clear()
            self._threads = []
            for i in range(max(1, self._max_concurrent)):
                t = threading.Thread(target=self._worker_loop, daemon=True,
                                     name=f"NOVATaskManager-{i}")
                t.start()
                self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()

    # ── persistence ─────────────────────────────────────────────────────────
    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._tasks = [Task.from_dict(t) for t in (data or [])]
            except Exception:
                self._tasks = []
        # A task that was mid-flight when NOVA stopped is not running now.
        # Left as RUNNING it would show as work forever and never finish.
        for t in self._tasks:
            if t.status in ACTIVE:
                t.status = "FAILED"
                t.reason_for_stop = "NOVA was closed while this was running"
                t.finished = t.finished or time.time()
                t.phase = ""

    def _save(self) -> None:
        with self._lock:
            try:
                payload = [t.to_dict() for t in self._tasks]
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2)
                # os.replace raises PermissionError on Windows when a concurrent
                # reader briefly holds the destination open; retry so terminal
                # statuses (e.g. COMPLETED) are never silently lost.
                for _attempt in range(10):
                    try:
                        os.replace(tmp, self.path)
                        return
                    except PermissionError:
                        time.sleep(0.02)
                os.replace(tmp, self.path)
            except Exception:
                log.debug("[TASK] could not save tasks", exc_info=True)

    # ── events, history, messages ───────────────────────────────────────────
    def _brief(self, t: Task) -> Dict[str, Any]:
        return {"id": t.id, "title": t.title, "status": t.status, "phase": t.phase,
                "steps_done": t.steps_done(), "steps_total": len(t.steps),
                "progress": t.progress_percent()}

    def _event(self, t: Task, kind: str, detail: str = "", **extra: Any) -> None:
        """Record *kind* in the task's history and tell the observer."""
        now = time.time()
        with self._lock:
            t.history.append({"ts": now, "event": kind, "detail": detail[:300]})
            del t.history[:-MAX_HISTORY]
            t.updated = now
            brief = self._brief(t)
        fn = self._on_event
        if fn is None:
            return
        try:
            fn({"type": kind, "task_id": t.id, "task": brief,
                "detail": detail[:300], "ts": now, **extra})
        except Exception:
            log.debug("[TASK] event observer failed", exc_info=True)

    def _message(self, t: Task, frm: str, to: str, kind: str, text: str,
                 step: int = -1, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """A structured message from one agent to another, kept on the task."""
        with self._lock:
            msg = {"id": f"m{len(t.agent_messages) + 1}", "ts": time.time(), "from": frm,
                   "to": to, "kind": kind, "text": text[:400], "step": step}
            if data:
                msg["data"] = data
            t.agent_messages.append(msg)
            del t.agent_messages[:-MAX_MESSAGES]
        self._event(t, "agent.message", f"{frm} → {to}: {text[:160]}", message=msg)
        return msg

    def _set_status(self, t: Task, status: str, phase: str = "") -> None:
        with self._lock:
            t.status = status
            t.phase = phase
            t.updated = time.time()
            self._save()
        self._event(t, "task.status", f"{status}{': ' + phase if phase else ''}")

    # ── task CRUD ───────────────────────────────────────────────────────────
    def submit(self, title: str, steps: List[Dict[str, Any]], meta: Optional[dict] = None,
               dependencies: Optional[List[str]] = None, priority: str = "NORMAL",
               task_id: Optional[str] = None,
               estimated_duration_s: float = 0.0, parent_task_id: str = "") -> Task:
        """steps: list of {"tool": ..., "args": {...}}. Returns the created Task.

        A task with nothing to run is a caller error, so say so -- it used to
        become a step naming a tool called "agent" that has never existed.
        To hand over a goal and let NOVA plan it, use submit_goal().

        estimated_duration_s: how long NOVA told the user this would take.
        Purely descriptive; nothing enforces it.
        """
        if not steps:
            raise ValueError(
                "a task needs at least one step: "
                '[{"tool": ..., "args": {...}}, ...]'
            )
        return self._add(Task(title=title,
                              steps=[TaskStep(tool=s["tool"], args=s.get("args", {}))
                                     for s in steps]),
                         dependencies, priority, task_id, estimated_duration_s, parent_task_id)

    def submit_goal(self, goal: str, *, title: str = "", priority: str = "NORMAL",
                    estimated_duration_s: float = 0.0, parent_task_id: str = "") -> Task:
        """Queue *goal* to be planned by a worker, not by the caller.

        Planning is a model call that can take many seconds. Done inline, as
        it was, the voice turn that asked for the work sat silent until it
        finished; now the reply is immediate and the plan follows.
        """
        goal = (goal or "").strip()
        if not goal:
            raise ValueError("a goal to plan is needed")
        t = Task(title=title or goal[:120], user_request=goal, needs_plan=True)
        return self._add(t, None, priority, None, estimated_duration_s, parent_task_id)

    def _add(self, t: Task, dependencies, priority, task_id, estimated_duration_s,
             parent_task_id) -> Task:
        with self._lock:
            t.dependencies = list(dependencies or [])
            t.priority = (priority or "NORMAL").upper()
            t.estimated_duration_s = max(0.0, float(estimated_duration_s or 0))
            t.parent_task_id = parent_task_id or ""
            t.user_request = t.user_request or t.title
            if task_id:
                t.id = task_id
            t.status = "QUEUED"
            t.phase = "Waiting to start"
            t.updated = time.time()
            self._tasks.append(t)
            self._save()
            self._cond.notify_all()
        self._event(t, "task.created", t.title)
        return t

    def get(self, task_id: str) -> Optional[Task]:
        for t in self._tasks:
            if t.id == task_id:
                return t
        return None

    def list(self, status: Optional[str] = None) -> List[Task]:
        if status:
            return [t for t in self._tasks if t.status == status.upper()]
        return list(self._tasks)

    def recent(self, n: int = 12) -> List[Task]:
        """Active and queued tasks first, then the most recently changed."""
        with self._lock:
            tasks = list(self._tasks)
        tasks.sort(key=lambda t: (t.status not in TERMINAL, t.updated), reverse=True)
        return tasks[:n]

    def cancel(self, task_id: str, force: bool = False) -> bool:
        with self._lock:
            t = self.get(task_id)
            if not t or t.status in TERMINAL:
                return False
            running = t.status in ACTIVE
            t.status = "CANCELLED"
            t.phase = ""
            # A running task stops at the next step boundary; the step in
            # hand finishes, since killing a tool mid-write is worse.
            t.reason_for_stop = ("cancelled by user (finishing current step)"
                                 if running else "cancelled by user")
            if not running:
                t.finished = time.time()
            t.updated = time.time()
            self._save()
        self._event(t, "task.cancelled", t.reason_for_stop)
        return True

    def retry(self, task_id: str) -> Optional[Task]:
        """Run a stopped task again as a new task whose parent is the old one.

        A new task rather than a rewind: the failed run stays on record with
        what went wrong, and the retry has its own history. Steps are replanned
        from the goal when there was one, since the plan may be what failed.
        """
        with self._lock:
            old = self.get(task_id)
            if old is None or old.status not in TERMINAL or old.status == "COMPLETED":
                return None
            goal, steps = old.user_request, [{"tool": s.tool, "args": dict(s.args)} for s in old.steps]
            # A body written from the old run's findings is not part of the
            # plan; the retry writes it from its own.
            for m in old.agent_messages:
                if m.get("kind") == "handoff" and 0 <= m.get("step", -1) < len(steps):
                    steps[m["step"]]["args"].pop("content", None)
            planned = (old.needs_plan or not steps
                       or any(h.get("event") == "task.planned" for h in old.history))
        if planned and goal:
            t = self.submit_goal(goal, title=old.title, parent_task_id=old.id)
        elif steps:
            t = self.submit(old.title, steps, parent_task_id=old.id,
                            estimated_duration_s=old.estimated_duration_s)
        else:
            return None
        self._event(old, "task.retried", f"as {t.id}")
        return t

    def pause(self, task_id: str) -> bool:
        with self._lock:
            t = self.get(task_id)
            if not (t and t.status == "QUEUED"):
                return False
            t.status = "PAUSED"
            t.reason_for_stop = "paused"
            t.phase = "Paused"
            t.updated = time.time()
            self._save()
        self._event(t, "task.paused")
        return True

    def resume(self, task_id: str) -> bool:
        with self._lock:
            t = self.get(task_id)
            # WAITING (with no step in hand) is how a paused task was stored
            # before PAUSED existed.
            if not (t and (t.status == "PAUSED"
                           or (t.status == "WAITING" and t.reason_for_stop == "paused"))):
                return False
            t.status = "QUEUED"
            t.reason_for_stop = ""
            t.phase = "Waiting to start"
            t.updated = time.time()
            self._cond.notify_all()
            self._save()
        self._event(t, "task.resumed")
        return True

    def progress_text(self) -> str:
        """Answer for: what are you working on? / how far? / what's running?"""
        with self._lock:
            running = [t for t in self._tasks if t.status not in TERMINAL]
            if not running:
                done = [t for t in self._tasks if t.status in TERMINAL]
                if done:
                    latest = max(done, key=lambda t: t.updated)
                    return (f"Nothing running right now. Last task '{latest.title}' "
                            f"ended as {latest.status}.")
                return "No tasks yet."
            return "\n".join(t.summary(detail=False) for t in running)

    # ── verification ────────────────────────────────────────────────────────
    def _verify_step(self, task: Task, step: TaskStep) -> str:
        """Returns verification status string. Never claims success without a signal."""
        text = (step.result or "").strip()
        if not text:
            return "UNKNOWN"
        if self._verify_fn is not None:
            try:
                return self._verify_fn(text)
            except Exception:
                pass
        low = text.lower()
        if step.tool in self.RESEARCH_TOOLS:
            return self._verify_search(low)
        if step.tool in WRITER_TOOLS:
            saved = self._verify_saved(text)
            if saved:
                return saved
        # "refused:" is how nova.py reports an authorisation denial.
        # Only the opening is judged: a failure is reported up front, and the
        # rest of a long result may quote anything ("... the error was ...").
        head = low[:200]
        failure = ["error:", "failed", "exception", "not found", "unavailable",
                   "denied", "blocked", "timeout", "unable to", "unknown ",
                   "refused:", "couldn't", "could not"]
        if head.startswith(self.DISPATCH_FAILURES) or any(f in head for f in failure):
            return "FAILURE"
        success = ["saved:", "saved ", "created:", "created ", "completed:", "submitted:",
                   "ready:", "✅", "remembered:", "applied", "results:", "found ",
                   "wrote:", "wrote ", "generated:", "validated:", "opened ",
                   "reminder set:", "memory store:", "queued:", "deleted:", "moved"]
        if any(s in low for s in success):
            return "CONFIRMED_SUCCESS"
        return "UNKNOWN"

    @staticmethod
    def _verify_saved(text: str) -> str:
        """A writer's own report of the file it saved, checked against the disk.

        "Saved <path> (N bytes)." never matched the old keyword "saved:", so
        every generated document came back UNVERIFIED and a task that had
        written it ended "partially completed, 1 step(s) not verified".
        """
        if text.lower().startswith("failed"):
            return "FAILURE"
        m = _SAVED.match(text.strip())
        if not m:
            return ""
        path = os.path.expanduser(m.group("path").strip())
        try:
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                return "CONFIRMED_SUCCESS"
        except OSError:
            pass
        return "FAILURE"

    #: How actions/web_search.py words a search that produced nothing. Its
    #: successful output is the findings themselves, so the generic keyword
    #: scan can't judge it: results about a failed launch contain "failed",
    #: and "No results found" contains the success keyword "found ".
    SEARCH_FAILURES = ("no results found for", "couldn't search",
                       "please provide a search query", "comparison failed")

    #: Prefixes nova.py's dispatcher puts on a tool that raised or was refused.
    DISPATCH_FAILURES = ("error", "web_search error", "refused:")

    @classmethod
    def _verify_search(cls, low: str) -> str:
        if low.startswith(cls.DISPATCH_FAILURES):
            return "FAILURE"
        # Only the opening: the rest is findings, which may quote anything.
        if any(f in low[:200] for f in cls.SEARCH_FAILURES):
            return "FAILURE"
        return "LIKELY_SUCCESS"

    def _validate_artifacts(self, task: Task) -> int:
        """Record files verified steps produced, checked for existence + size."""
        validated = 0
        for i, step in enumerate(task.steps):
            # Findings name files and links about the world, not files made.
            if step.status != "VERIFIED" or step.tool in RESEARCH_TOOLS:
                continue
            paths = []
            m = _SAVED.match((step.result or "").strip())
            if m:
                paths.append(os.path.expanduser(m.group("path").strip()))
            for text in (step.result or "", json.dumps(step.args, default=str)):
                paths.extend(_extract_paths(text))
            for p in dict.fromkeys(paths):
                norm = os.path.normpath(p)
                if not os.path.isfile(norm):
                    continue
                if any(a.get("path") in (p, norm) for a in task.artifacts):
                    continue
                size = os.path.getsize(norm)
                art = {"path": norm, "exists": True, "size": size, "validated": size > 0,
                       "step": i, "agent": step.agent}
                task.artifacts.append(art)
                validated += 1
                self._event(task, "artifact.created", norm, artifact=art)
        return validated

    # ── worker ──────────────────────────────────────────────────────────────
    def _deps_ready(self, t: Task) -> bool:
        for dep_id in t.dependencies:
            dep = self.get(dep_id)
            if dep is None or dep.status not in ("COMPLETED",):
                return False
        return True

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            t = None
            with self._cond:
                while not self._stop.is_set():
                    t = next((x for x in self._tasks
                              if x.status == "QUEUED" and self._deps_ready(x)), None)
                    if t is not None:
                        break
                    self._cond.wait(1.0)
                if self._stop.is_set():
                    return
                t.status = "PLANNING" if t.needs_plan else "RUNNING"
                t.phase = "Planning" if t.needs_plan else "Starting"
                t.started = t.started or time.time()
                t.updated = time.time()
                self._save()
            self._event(t, "task.started", t.phase)
            self._signal_activity(True)
            try:
                self._run_task(t)
            except Exception as e:
                log.exception("[TASK] %s crashed", t.id)
                t.errors.append(str(e))
                self._finish(t, "FAILED", f"crash: {e}")
            finally:
                # Released on every exit, including a crash: an orb left
                # showing work that has stopped makes the user wait for
                # something that is not coming. With a pool, one worker
                # finishing is not the same as all work finishing.
                with self._lock:
                    more = (any(x.status in ACTIVE for x in self._tasks)
                            or any(x.status == "QUEUED" and self._deps_ready(x)
                                   for x in self._tasks))
                if not more:
                    self._signal_activity(False)

    def _plan(self, t: Task) -> bool:
        """Turn the task's goal into steps. False when nothing could be planned."""
        work = agent_activity.begin("orchestrator", f"Planning: {t.title}",
                                    source="task", task_id=t.id)
        try:
            steps = self.plan_steps(t.user_request or t.title)
        finally:
            agent_activity.end(work)
        steps = [s for s in steps if isinstance(s, dict) and s.get("tool")]
        if not steps:
            return False
        with self._lock:
            t.steps = [TaskStep(tool=s["tool"], args=dict(s.get("args") or {})) for s in steps]
            t.needs_plan = False
            t.updated = time.time()
            self._save()
        self._event(t, "task.planned", f"{len(t.steps)} step(s): "
                    + "; ".join(s.description for s in t.steps)[:280])
        return True

    def _run_task(self, t: Task) -> None:
        tool_exec = self._tool_executor
        if tool_exec is None:
            self._finish(t, "FAILED", "no tool executor configured")
            return
        if t.needs_plan and not self._plan(t):
            self._finish(t, "FAILED" if t.status != "CANCELLED" else "CANCELLED",
                         None if t.status == "CANCELLED" else
                         f"could not plan '{t.user_request or t.title}' into steps NOVA can run")
            return
        if t.status == "CANCELLED":
            self._finish(t, "CANCELLED")
            return
        self._set_status(t, "RUNNING", "Starting")

        for i in range(len(t.steps)):
            if t.status == "CANCELLED":
                break
            self._run_step(t, i, tool_exec)
            if t.steps[i].status == "FAILED":
                break

        if t.status != "CANCELLED" and not any(s.status == "FAILED" for s in t.steps):
            self._review_loop(t, tool_exec)
        self._conclude(t)

    # ── one step ────────────────────────────────────────────────────────────
    def _run_step(self, t: Task, i: int, tool_exec: Callable) -> None:
        step = t.steps[i]
        n = len(t.steps)
        self._inject_context(t, i)
        with self._lock:
            step.status = "RUNNING"
            step.started = time.time()
            step.error = ""
            t.current_step = i
            if t.status != "CANCELLED":
                t.phase = step.description
            t.updated = time.time()
            self._save()
        self._message(t, "orchestrator", step.agent, "assign",
                      f"Step {i + 1}/{n}: {step.description}", step=i)
        self._event(t, "task.step", f"{i + 1}/{n} {step.agent}: {step.description}", step=i)

        lock = self._acquire_resource(t, step)
        if lock is False:           # cancelled while waiting for the resource
            with self._lock:
                step.status = "SKIPPED"
                step.finished = time.time()
            return
        work = agent_activity.begin(step.agent, step.description, source="task", task_id=t.id)
        try:
            # One retry on an exception -- a transient network or device error
            # is common. A retry that succeeds is judged like any other result;
            # it used to stay FAILED, so the task failed on work that had worked.
            for attempt in range(2):
                try:
                    result = tool_exec(step.tool, step.args, {"task_id": t.id, "background": True})
                    step.result = (result if isinstance(result, str) else str(result))[:4000]
                    step.error = ""
                    break
                except Exception as e:
                    step.result = ""
                    step.error = str(e)[:500]
                    t.errors.append(f"step {i + 1} ({step.tool}): {e}")
                    if attempt == 0:
                        step.retries += 1
                        self._event(t, "task.retry", f"step {i + 1} ({step.tool}): {e}", step=i)
        finally:
            agent_activity.end(work)
            if lock is not None:
                lock.release()

        step.finished = time.time()
        if step.error and not step.result:
            step.status, step.verification = "FAILED", "FAILURE"
        else:
            ver = self._verify_step(t, step)
            step.verification = ver
            step.verified = ver in ("CONFIRMED_SUCCESS", "LIKELY_SUCCESS")
            if step.verified:
                step.status = "VERIFIED"
            elif ver == "FAILURE":
                # A step whose output says it failed did not merely go
                # unverified -- it failed, and the task must say so.
                step.status = "FAILED"
            else:
                step.status = "UNVERIFIED"
        outcome = step.error or step.result[:160] or "no output"
        self._message(t, step.agent, "orchestrator",
                      "result" if step.status != "FAILED" else "failure",
                      f"{step.status.lower()}: {outcome}", step=i)
        with self._lock:
            self._validate_artifacts(t)
            t.progress = t.progress_percent()
            t.updated = time.time()
            self._save()
        self._event(t, "task.progress", f"{t.steps_done()}/{n} steps", step=i)

    def _acquire_resource(self, t: Task, step: TaskStep):
        """The shared resource *step* needs (desktop, browser), held exclusively.

        Returns the held lock, None when the step needs none, or False when
        the task was cancelled while it waited. Two tasks driving the mouse
        at once would each ruin the other's work.
        """
        res = agent_activity.resource_for_tool(step.tool)
        if not res:
            return None
        lock = agent_activity.resource_lock(res)
        if lock.acquire(blocking=False):
            return lock
        with self._lock:
            step.status = "WAITING"
            if t.status == "RUNNING":
                t.status = "WAITING"
            t.phase = f"Waiting for the {res} (in use by other work)"
            self._save()
        self._event(t, "task.waiting", t.phase)
        while not lock.acquire(timeout=1.0):
            if t.status == "CANCELLED" or self._stop.is_set():
                return False
        with self._lock:
            step.status = "RUNNING"
            if t.status == "WAITING":
                t.status = "RUNNING"
            t.phase = step.description
            self._save()
        self._event(t, "task.status", f"RUNNING: {step.description}")
        return lock

    def _inject_context(self, t: Task, i: int) -> None:
        """Hand what earlier steps found to a step that needs it.

        The model plans a document step before any research has run, so it
        cannot write the body -- its own tool description showed
        "content": "...", and that is what got saved. When a writing step has
        no real body, it is written from the verified research before it,
        and the hand-off is recorded as a message between the two agents.
        """
        step = t.steps[i]
        found = [(j, s) for j, s in enumerate(t.steps[:i])
                 if s.tool in RESEARCH_TOOLS and s.status == "VERIFIED" and s.result]
        # {{step_N}} / {{previous}} / {{results}} placeholders in any string arg.
        prev = t.steps[i - 1].result if i > 0 else ""
        research = "\n\n".join(s.result for _, s in found)
        for k, v in list(step.args.items()):
            if k != "content" and isinstance(v, str) and "{{" in v:
                v = v.replace("{{previous}}", prev).replace("{{results}}", research)
                v = re.sub(r"\{\{step_(\d+)\}\}",
                           lambda m: (t.steps[int(m.group(1)) - 1].result
                                      if 0 < int(m.group(1)) <= i else ""), v)
                step.args[k] = v
        if not (_writes_file(step) and found and _placeholder(step.args.get("content"))):
            return
        # No title heading: the writer puts the document's title on it.
        parts: List[str] = []
        for j, s in found:
            q = s.args.get("query") or s.args.get("q") or f"Search {j + 1}"
            parts += [f"## {q}", "", s.result.strip(), ""]
        step.args["content"] = "\n".join(parts).strip()
        self._message(t, "research", step.agent, "handoff",
                      f"{len(found)} set(s) of findings ({len(step.args['content'])} chars) "
                      f"for step {i + 1}", step=i,
                      data={"from_steps": [j for j, _ in found]})

    # ── review ──────────────────────────────────────────────────────────────
    def _review(self, t: Task, rnd: int) -> Dict[str, Any]:
        """Check the finished work. Returns a ReviewResult.

        Every check is against something real -- the disk, the step results --
        never a model's opinion of itself. Each issue names the step and the
        agent that should fix it.
        """
        issues: List[Dict[str, Any]] = []
        checked: List[str] = []
        for i, s in enumerate(t.steps):
            if s.tool in RESEARCH_TOOLS:
                checked.append(f"step {i + 1}: the search returned findings")
                if s.status != "VERIFIED":
                    issues.append({"step": i, "agent": s.agent,
                                   "problem": "the search found nothing usable",
                                   "fix": "search again"})
            elif _writes_file(s):
                checked.append(f"step {i + 1}: the file exists, is not empty, and has real content")
                m = _SAVED.match((s.result or "").strip())
                path = os.path.expanduser(m.group("path").strip()) if m else ""
                on_disk = bool(path) and os.path.isfile(path) and os.path.getsize(path) > 0
                if s.status != "VERIFIED" or (m and not on_disk):
                    issues.append({"step": i, "agent": s.agent,
                                   "problem": "the file was not written or is empty",
                                   "fix": "write it again"})
                elif "content" in s.args and _placeholder(s.args.get("content")):
                    has_findings = any(r.tool in RESEARCH_TOOLS and r.status == "VERIFIED"
                                       for r in t.steps[:i])
                    issues.append({"step": i, "agent": s.agent,
                                   "problem": "the document has no real content",
                                   # Without findings there is nothing to write
                                   # it from: asking again would only repeat it.
                                   "fix": ("write it from the research findings"
                                           if has_findings else "needs content from the user"),
                                   "retry": has_findings})
        # Work that falls in a domain the person taught NOVA is also held to
        # what they taught (nova_learning, §103). Only verified domains count,
        # and every issue names the learned principle and the file it came from.
        for i, s in enumerate(t.steps):
            content = str(s.args.get("content") or "") if _writes_file(s) else ""
            if not content or s.status != "VERIFIED" or _placeholder(content):
                continue
            try:
                from nova_learning import retrieve as _learned
                breaks = _learned.check_against(content, t.title)
            except Exception as e:
                log.info("learned-principle review skipped: %s", e)
                continue
            if breaks is None:
                continue                    # nothing the person taught bears on this
            checked.append(f"step {i + 1}: checked against the principles you taught me")
            for b in breaks[:5]:
                issues.append({"step": i, "agent": s.agent,
                               "problem": (f"it breaks a learned principle: {b['principle']} "
                                           f"(from {', '.join(b['sources'][:2])}) — {b['why']}"),
                               "fix": b["fix"] or f"revise it to follow: {b['principle']}",
                               "retry": True, "learned": b})
        return {"round": rnd, "passed": not issues, "issues": issues,
                "checked": checked, "ts": time.time()}

    def _review_loop(self, t: Task, tool_exec: Callable) -> None:
        """Review, send each problem to the agent that owns it, redo, re-review."""
        if not any(s.tool in RESEARCH_TOOLS or _writes_file(s) for s in t.steps):
            return      # nothing a review can check -- say nothing rather than rubber-stamp
        for rnd in range(1, MAX_REVIEW_ROUNDS + 2):
            if t.status == "CANCELLED":
                return
            self._set_status(t, "REVIEWING", "Reviewing the work")
            work = agent_activity.begin("reviewer", f"Reviewing: {t.title}",
                                        source="task", task_id=t.id)
            try:
                result = self._review(t, rnd)
            finally:
                agent_activity.end(work)
            with self._lock:
                t.reviews.append(result)
                self._save()
            self._event(t, "review.completed",
                        "passed" if result["passed"] else
                        "; ".join(x["problem"] for x in result["issues"]), review=result)
            if result["passed"] or rnd > MAX_REVIEW_ROUNDS:
                return
            fixable = [x for x in result["issues"] if x.get("retry", True)]
            if not fixable:
                return
            for issue in fixable:
                if t.status == "CANCELLED":
                    return
                i = issue["step"]
                self._message(t, "reviewer", issue["agent"], "review_feedback",
                              f"Step {i + 1}: {issue['problem']} — {issue['fix']}",
                              step=i, data=issue)
                step = t.steps[i]
                if "research" in issue["fix"] and step.tool in WRITER_TOOLS:
                    step.args["content"] = ""       # re-filled from the findings
                elif issue.get("learned") and step.args.get("content"):
                    # Rewriting the same text would fail the same check: revise
                    # it to follow the learned principle it broke.
                    try:
                        from nova_learning import retrieve as _learned
                        step.args["content"] = _learned.revise(step.args["content"], [issue["learned"]])
                    except Exception as e:
                        log.info("could not revise to the learned principle: %s", e)
                self._set_status(t, "RUNNING", f"Revising: {step.description}")
                with self._lock:
                    step.status, step.result, step.verification = "QUEUED", "", "UNKNOWN"
                self._run_step(t, i, tool_exec)

    # ── conclusion ──────────────────────────────────────────────────────────
    def _conclude(self, t: Task) -> None:
        """Final status -- no false completion."""
        failed = [s for s in t.steps if s.status == "FAILED"]
        verified = [s for s in t.steps if s.status == "VERIFIED"]
        unverified = [s for s in t.steps if s.status == "UNVERIFIED"]
        review = t.reviews[-1] if t.reviews else None
        if t.status == "CANCELLED":
            self._finish(t, "CANCELLED")
        elif failed:
            self._finish(t, "FAILED", f"step failed: {failed[0].tool} — "
                                      f"{failed[0].error or failed[0].result[:120]}")
        elif review is not None and not review["passed"]:
            self._finish(t, "PARTIALLY_COMPLETED", "review found: "
                         + "; ".join(f"step {x['step'] + 1} {x['problem']}" for x in review["issues"]))
        elif unverified and verified:
            self._finish(t, "PARTIALLY_COMPLETED", f"{len(unverified)} step(s) not verified: "
                         + ", ".join(s.tool for s in unverified))
        elif unverified:
            self._finish(t, "UNVERIFIED", "steps ran but success could not be verified")
        elif verified:
            self._finish(t, "COMPLETED", "")
        else:
            self._finish(t, "UNVERIFIED", "no steps executed")

    def _finish(self, t: Task, status: str, reason: Optional[str] = None) -> None:
        with self._lock:
            t.status = status
            if reason is not None:
                t.reason_for_stop = reason
            t.phase = ""
            t.progress = 100 if status == "COMPLETED" else t.progress_percent()
            t.finished = time.time()
            t.updated = time.time()
            self._save()
        kind = {"COMPLETED": "task.completed", "CANCELLED": "task.cancelled",
                "FAILED": "task.failed"}.get(status, "task.finished")
        self._event(t, kind, t.reason_for_stop or status)
        if status != "CANCELLED":
            self._remember_research(t)
            self._notify_done(t)

    #: Tools whose output is findings about the world, worth keeping so the
    #: same topic is not researched from scratch next time.
    RESEARCH_TOOLS = RESEARCH_TOOLS

    def _remember_research(self, t: Task) -> None:
        """File what a task's verified searches found under its title."""
        found = [s.result for s in t.steps
                 if s.tool in self.RESEARCH_TOOLS and s.status == "VERIFIED" and s.result]
        if not found:
            return
        try:
            from living_memory import get_living_memory
            mem = get_living_memory()
            if mem is None:
                return
            text = "\n\n".join(found)
            sources = list(dict.fromkeys(re.findall(r"https?://[^\s)\]>\"']+", text)))
            mem.remember_research(t.title, text, sources=sources[:20])
        except Exception as e:
            log.debug("could not file research for %s: %s", t.id, e)

    @staticmethod
    def _prior_research(title: str) -> str:
        """What is already known about *title*, as a note for the model."""
        try:
            from living_memory import get_living_memory
            mem = get_living_memory()
            hits = mem.recall_research(title, top_k=1) if mem is not None else []
        except Exception:
            return ""
        if not hits:
            return ""
        when = time.strftime("%Y-%m-%d", time.localtime(
            hits[0].get("updated") or hits[0].get("created") or time.time()))
        return (f" You already researched this on {when}; what you found then: "
                f"{hits[0]['text'][:600]}")

    def _notify_done(self, t: Task) -> None:
        if not t.notify_done:
            return
        # Said in NOVA's voice, owning the result and what review caught
        # (nova_personality) -- not a status line from a dashboard.
        try:
            msg = _persona.task_outcome_message(
                t.title, t.status, t.reason_for_stop, t.artifacts, t.reviews)
        except Exception:
            msg = f"Task '{t.title}' {t.status.lower().replace('_', ' ')}."
        fn = self._notify
        if fn is not None:
            try:
                fn(msg)
            except Exception:
                pass
        # memory integration — task outcome becomes recallable memory
        try:
            from living_memory import get_living_memory
            mem = get_living_memory()
            if mem is not None:
                outcome = f"Task '{t.title}' ended as {t.status}."
                if t.artifacts:
                    paths = ", ".join(a["path"] for a in t.artifacts)
                    outcome += f" Artifacts: {paths}"
                mem.remember_task_outcome(outcome)
        except Exception:
            pass

    # ── command handling for the model/REPL ─────────────────────────────────
    #: Turns a goal into steps when the caller gave none. Replaceable (tests).
    planner: Optional[Callable[[str], List[Dict[str, Any]]]] = None

    def plan_steps(self, goal: str) -> List[Dict[str, Any]]:
        """Steps for a goal: NOVA's planner, else its keyword fallback, else []."""
        if self.planner is not None:
            try:
                return list(self.planner(goal) or [])
            except Exception as e:
                log.warning("task planner failed for %r: %s", goal, e)
                return []
        import sys
        nova_mod = sys.modules.get("nova")
        declarations = list(getattr(nova_mod, "TOOL_DECLARATIONS", None) or [])
        try:
            from agent import planner as _planner
            try:
                plan = (_planner.create_plan(goal, tool_declarations=declarations)
                        if declarations else _planner.create_plan(goal))
            except Exception as e:
                log.warning("planning %r failed (%s); using the fallback plan", goal, e)
                plan = None
        except Exception as e:
            log.warning("no planner available for %r: %s", goal, e)
            plan = None
        steps = []
        for st in (plan or {}).get("steps") or []:
            tool = st.get("tool")
            if tool:
                steps.append({"tool": tool, "args": dict(st.get("parameters") or st.get("args") or {})})
        runnable = [s for s in steps if self._can_run(s["tool"], declarations)]
        if len(runnable) < len(steps):
            log.warning("plan for %r named tools NOVA does not have: %s", goal,
                        sorted({s["tool"] for s in steps} - {s["tool"] for s in runnable}))
        return runnable or self._fallback_steps(goal)

    @staticmethod
    def _can_run(tool: str, declarations: List[Dict[str, Any]]) -> bool:
        """A step NOVA can dispatch and authorise. Anything else is refused at
        run time and fails the task, so it must not be planned."""
        if declarations and not any(d.get("name") == tool for d in declarations):
            return False
        try:
            from nova_core.permissions import capabilities_for_tool
            return capabilities_for_tool(tool) is not None
        except Exception:
            return True

    #: Words that mean the person wants something written, not only found.
    _WRITE_WORDS = ("report", "document", "essay", "thesis", "write", "summary",
                    "summarise", "summarize", "brief", "paper", "notes")

    def _fallback_steps(self, goal: str) -> List[Dict[str, Any]]:
        """When no plan could be made: search, and write it up if asked to.

        The planner's own fallback guesses tools from keywords -- "start"
        opened an app with no name -- and names tools NOVA no longer has."""
        steps: List[Dict[str, Any]] = [{"tool": "web_search", "args": {"query": goal[:200]}}]
        if any(w in goal.lower() for w in self._WRITE_WORDS):
            steps.append({"tool": "generate_document",
                          "args": {"title": goal[:80], "format": "docx"}})
        return steps

    def exec_command(self, cmd: str, task_id: str = "", title: str = "",
                     steps: Optional[List[Dict[str, Any]]] = None,
                     meta: Optional[dict] = None,
                     estimated_duration_s: float = 0.0) -> str:
        c = (cmd or "").strip().lower()
        if c in ("create", "add", "submit", "run"):
            goal = (title or "").strip()
            if not steps and goal and goal != "Untitled task":
                # The model often names the goal and leaves the steps to us
                # ("research exoplanets"). Refusing that meant nothing started
                # while NOVA said it had; planning it inline kept the voice
                # turn silent for as long as the planner took. A worker plans it.
                t = self.submit_goal(goal, estimated_duration_s=estimated_duration_s)
                return (f"Task {t.id} queued: '{t.title}'. It is being planned and will run "
                        f"in the background; the user will be told when it finishes. "
                        f"Carry on the conversation.")
            if not steps:
                return ("nova_task submit needs a title (a goal NOVA can plan) or a 'steps' "
                        'list, e.g. {"steps": [{"tool": "web_search", '
                        '"args": {"query": "..."}}]}. Nothing was started.')
            t = self.submit(title or "Untitled task", steps, meta=meta,
                            estimated_duration_s=estimated_duration_s)
            prior = (self._prior_research(t.title)
                     if any(s.tool in self.RESEARCH_TOOLS for s in t.steps) else "")
            return (f"Task {t.id} queued: '{t.title}' "
                    f"({len(t.steps)} step(s)). You'll be notified when it finishes."
                    + prior)
        if c in ("status", "progress", "what"):
            return self.progress_text()
        if c in ("list", "all"):
            lines = [t.summary(detail=True) for t in self._tasks[-8:]]
            return "\n".join(lines) if lines else "No tasks yet."
        if c in ("cancel", "stop"):
            if not task_id:
                # cancel everything running
                n = sum(1 for t in list(self._tasks) if self.cancel(t.id, force=True))
                return f"Cancelled {n} task(s)."
            return "Cancelled." if self.cancel(task_id, force=True) else f"No active task {task_id}."
        if c in ("pause",):
            return "Paused." if self.pause(task_id) else f"No queued task {task_id}."
        if c in ("resume", "continue"):
            return "Resumed." if self.resume(task_id) else f"No paused task {task_id}."
        raise ValueError(f"unknown task command: {cmd}")


# ── module singleton ──────────────────────────────────────────────────────────
_singleton: Optional[TaskManager] = None


def init_task_manager(path: Optional[str] = None,
                      tool_executor: Optional[Callable] = None,
                      verify_fn: Optional[Callable] = None,
                      auto_start: bool = True,
                      max_concurrent: int = 3) -> TaskManager:
    global _singleton
    _singleton = TaskManager(path=path or None,
                             tool_executor=tool_executor,
                             verify_fn=verify_fn,
                             max_concurrent=max_concurrent)
    if auto_start:
        _singleton.start()
    return _singleton


def get_task_manager() -> Optional[TaskManager]:
    return _singleton
