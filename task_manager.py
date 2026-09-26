"""
task_manager.py
════════════════
AIOS Concurrent Task Manager — background multi-step tasks that NEVER block the
voice/conversation loop, with verified-completion semantics.

Design (per NOVA AIOS spec §102–113):
  • Runs on its own daemon thread with a work queue → the Live asyncio loop and
    offline loop stay responsive while tasks execute.
  • Task state machine: PLANNED → QUEUED → RUNNING → (WAITING/VERIFYING) →
    COMPLETED | FAILED | CANCELLED | PARTIALLY_COMPLETED | UNVERIFIED.
  • NO FALSE COMPLETION: a task is only COMPLETED after every step is verified
    (CONFIRMED_SUCCESS / LIKELY_SUCCESS / or an explicit file-exists+size check).
    Otherwise it is PARTIALLY_COMPLETED / UNVERIFIED / FAILED — never "Done".
  • Dependencies between tasks, per-task priority, cancel/pause/resume.
  • Artifact registry: files a task created, validated for existence + size.
  • Persistence to nova_tasks_aios.json (atomic write) → survives restart.
  • Events/progress exposed for "what are you working on?" / "how far?" / etc.
  • Notify via an injected speak callable (non-blocking) + memory integration
    (task outcomes stored so they're recallable later).

Import-safe: does NOT import nova at module top; the tool-executor is injected,
so it can be unit-tested standalone.
"""
from __future__ import annotations
import json, logging, os, re, threading, time, uuid
from dataclasses import dataclass, field, fields
from typing import Any, Callable, Dict, List, Optional

import nova_paths

log = logging.getLogger("nova.task")

__all__ = ["TaskManager", "TaskStep", "get_task_manager", "init_task_manager"]

TASK_STATUSES = ["PLANNED", "QUEUED", "RUNNING", "WAITING", "VERIFYING",
                 "COMPLETED", "FAILED", "CANCELLED", "PARTIALLY_COMPLETED",
                 "UNVERIFIED"]
STEP_STATUSES = ["QUEUED", "RUNNING", "VERIFIED", "UNVERIFIED", "FAILED",
                 "SKIPPED"]

TASKS_FILENAME = "nova_tasks_aios.json"


def default_tasks_path() -> str:
    """Resolved per call, not at import.

    This was anchored to the module's own directory, which for a frozen
    install is the install directory — not writable by a standard user, and
    `_save()` swallows every exception, so tasks would have failed to persist
    in silence. In development it is the repository, which is how one user's
    tasks came to be a tracked file.
    """
    return str(nova_paths.data_file(TASKS_FILENAME))

# tool names that produce filesystem artifacts we should validate
_FILE_TOOLS = {"file_processor", "file_controller", "self_editor", "web_search",
               "document", "pdf", "report"}


def _extract_paths(text: str) -> List[str]:
    """Best-effort extraction of file-ish paths from a string."""
    out = []
    for m in re.finditer(r"(?:[A-Za-z]:\\|/|\./|~/)[^\s\"']{3,180}", text):
        p = m.group(0).rstrip(".,;:)]}")
        if p and not p.endswith((".com", ".net", ".org", ".html", ".py:")):
            out.append(p)
    return out[:10]


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
    #: Seconds the submitter (typically NOVA herself, having just told the
    #: user "give me about ten minutes") expects this to take. 0 means no
    #: estimate was given -- progress() then falls back to a step-count
    #: ratio instead of a time-based one, since there is nothing to
    #: measure time against.
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

    def to_dict(self) -> Dict[str, Any]:
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
        """0-100, computed fresh every time it's asked rather than read
        off a field nothing ever updated (self.progress used to be set
        exactly once, to 100, at COMPLETED -- 0% for the entire time a
        task was actually running, whatever it was doing).

        Time-based while RUNNING with an estimate (nova_task can be given
        one when the model told the user "give me about ten minutes"),
        capped at 95 until the task actually finishes -- an estimate is a
        guess, and claiming 100% before real completion would be a
        second, smaller version of the exact "false completion" problem
        this whole module exists to prevent for step verification. Falls
        back to a step-count ratio when there is no time estimate to
        measure against, and to whatever was last recorded once the task
        has actually stopped.
        """
        if self.status == "COMPLETED":
            return 100
        if self.status in ("FAILED", "CANCELLED", "PARTIALLY_COMPLETED", "UNVERIFIED"):
            return self.progress
        if self.status == "RUNNING" and self.started:
            if self.estimated_duration_s > 0:
                elapsed = time.time() - self.started
                return max(0, min(95, int(100 * elapsed / self.estimated_duration_s)))
            if self.steps:
                done = sum(1 for s in self.steps if s.status in
                          ("VERIFIED", "SKIPPED", "FAILED"))
                return max(0, min(95, int(100 * done / len(self.steps))))
        return self.progress

    def seconds_remaining(self) -> Optional[float]:
        """None when there is nothing to count down -- no estimate was
        given, or the task is not (yet, or any longer) running. The UI's
        progress bar/countdown is meant to be honest about not knowing,
        not show a bar frozen at some arbitrary point."""
        if self.status != "RUNNING" or self.estimated_duration_s <= 0 or not self.started:
            return None
        return max(0.0, self.estimated_duration_s - (time.time() - self.started))

    def summary(self, detail: bool = False) -> str:
        done = sum(1 for s in self.steps if s.status in ("VERIFIED", "SKIPPED"))
        total = len(self.steps)
        lines = [f"{self.id} · {self.title} · {self.status} ({self.progress_percent()}%)"]
        if detail and self.steps:
            lines.append("  Steps:")
            for s in self.steps:
                lines.append(f"    [{s.status}] {s.tool} {json.dumps(s.args, default=str)[:60]}"
                             f"{' → ' + s.verification if s.result else ''}")
        elif self.steps:
            lines.append(f"  Step {self.current_step + 1}/{total} ({done} verified)")
        if self.reason_for_stop:
            lines.append(f"  Reason: {self.reason_for_stop}")
        return "\n".join(lines)


class TaskManager:
    """Background task executor running on a dedicated daemon thread."""

    def __init__(self, path: Optional[str] = None,
                 tool_executor: Optional[Callable[[str, Dict, Dict], Any]] = None,
                 verify_fn: Optional[Callable[[str], str]] = None,
                 max_concurrent: int = 3) -> None:
        self.path = path or default_tasks_path()
        self._tool_executor = tool_executor      # fn(tool_name, args, meta) -> result str
        self._verify_fn = verify_fn              # fn(output_text) -> verification status
        self._notify: Optional[Callable[[str], None]] = None
        self._on_activity: Optional[Callable[[bool], None]] = None
        self._speak = None
        self._lock = threading.RLock()
        self._tasks: List[Task] = []
        # A worker pool, not one dedicated thread: independent tasks (no
        # shared dependency chain) can now genuinely run at the same
        # time instead of queueing behind each other one at a time. 3 by
        # default -- this runs on the same machine as everything else
        # NOVA does, including local model inference, so unbounded
        # concurrency would compete with itself for the same CPU/RAM a
        # local LLM fallback needs.
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
        The desktop surface uses it to show that NOVA is working: a task now
        runs for a while off the back of a single spoken request, and an orb
        sitting at "listening" throughout is indistinguishable from NOVA
        having ignored the person.
        """
        self._on_activity = fn

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
        """Start the worker pool -- self._max_concurrent threads, each
        running the identical claim-a-task/run-it/loop-again cycle.

        This module's own docstring has called it a "Concurrent Task
        Manager" since it was written; the implementation was a single
        dedicated thread pulling one task at a time, which is the
        opposite of that. Independent tasks (no shared dependency chain
        -- see _deps_ready) can genuinely run at the same time; nothing
        about the claim logic below assumed only one worker would ever
        be doing it, because the claim itself (check status, flip to
        RUNNING) already happens under self._lock/self._cond -- the
        thing that made it correct for one worker also makes it correct
        for several.
        """
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

    def _save(self) -> None:
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump([t.to_dict() for t in self._tasks], f, indent=2)
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
            pass

    # ── task CRUD ───────────────────────────────────────────────────────────
    def submit(self, title: str, steps: List[Dict[str, Any]], meta: Optional[dict] = None,
               dependencies: Optional[List[str]] = None, priority: str = "NORMAL",
               task_id: Optional[str] = None,
               estimated_duration_s: float = 0.0) -> Task:
        """steps: list of {"tool": ..., "args": {...}}. Returns the created Task.

        A task with no steps used to become a single step naming a tool called
        "agent", which has never existed in NOVA. The permission engine
        fail-closed on it, and the task was reported to the user as unverified
        rather than as the empty submission it was. A task with nothing to run
        is a caller error, so say so.

        estimated_duration_s: how long the submitter (typically NOVA
        herself, having just told the user roughly how long this would
        take) expects it to run. Purely descriptive -- nothing here
        enforces it as a deadline -- and drives Task.progress_percent()'s
        time-based estimate and the UI's countdown; omit it for a task
        with no meaningful duration to estimate.
        """
        if not steps:
            raise ValueError(
                "a task needs at least one step: "
                '[{"tool": ..., "args": {...}}, ...]'
            )
        with self._lock:
            t = Task(title=title, steps=[TaskStep(tool=s["tool"], args=s.get("args", {}))
                                        for s in steps],
                     dependencies=list(dependencies or []), priority=priority.upper(),
                     estimated_duration_s=max(0.0, estimated_duration_s))
            if task_id:
                t.id = task_id
            t.status = "QUEUED"
            t.updated = time.time()
            self._tasks.append(t)
            self._save()
            self._cond.notify_all()
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

    def cancel(self, task_id: str, force: bool = False) -> bool:
        with self._lock:
            t = self.get(task_id)
            if not t or t.status in ("COMPLETED", "CANCELLED", "FAILED"):
                return False
            if t.status == "RUNNING" and not force:
                # will be picked up at the next step boundary
                t.status = "CANCELLED"
                t.reason_for_stop = "cancelled by user (finishing current step)"
                return True
            t.status = "CANCELLED"
            t.finished = time.time()
            t.reason_for_stop = "cancelled by user"
            t.updated = time.time()
            self._save()
            return True

    def pause(self, task_id: str) -> bool:
        with self._lock:
            t = self.get(task_id)
            if t and t.status == "QUEUED":
                t.status = "WAITING"
                t.reason_for_stop = "paused"
                t.updated = time.time()
                self._save()
                return True
        return False

    def resume(self, task_id: str) -> bool:
        with self._lock:
            t = self.get(task_id)
            if t and t.status == "WAITING":
                t.status = "QUEUED"
                t.reason_for_stop = ""
                t.updated = time.time()
                self._cond.notify_all()
                self._save()
                return True
        return False

    def progress_text(self) -> str:
        """Answer for: what are you working on? / how far? / what's running?"""
        with self._lock:
            running = [t for t in self._tasks
                       if t.status in ("QUEUED", "RUNNING", "VERIFYING", "WAITING")]
            if not running:
                done = [t for t in self._tasks
                        if t.status in ("COMPLETED", "FAILED", "CANCELLED",
                                        "PARTIALLY_COMPLETED", "UNVERIFIED")]
                if done:
                    latest = done[-1]
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
        # "refused:" is how nova.py reports an authorisation denial. Without it
        # a categorically refused step verified as UNKNOWN, and the task was
        # reported as "steps ran but success could not be verified" — when in
        # fact nothing ran at all.
        failure = ["error:", "failed", "exception", "not found", "unavailable",
                   "denied", "blocked", "timeout", "unable to", "unknown ",
                   "refused:"]
        for f in failure:
            if f in low:
                return "FAILURE"
        success = ["saved:", "created:", "completed:", "submitted:", "ready:",
                   "✅", "remembered:", "applied", "results:", "found ",
                   "wrote:", "wrote ", "generated:", "validated:",
                   "reminder set:", "memory store:", "queued:"]
        for s in success:
            if s in low:
                return "CONFIRMED_SUCCESS"
        return "UNKNOWN"

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
        """Check that files claimed to be created actually exist & are non-empty."""
        validated = 0
        for step in task.steps:
            if step.status != "VERIFIED":
                continue
            for text in (step.result or "", json.dumps(step.args, default=str)):
                for p in _extract_paths(text):
                    norm = os.path.normpath(p)
                    if os.path.isfile(norm):
                        size = os.path.getsize(norm)
                        exists = {"path": p, "exists": True, "size": size,
                                  "validated": size > 0}
                        if exists not in task.artifacts:
                            task.artifacts.append(exists)
                            validated += 1
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
                t.status = "RUNNING"
                t.started = t.started or time.time()
                t.updated = time.time()
                self._save()
            self._signal_activity(True)
            try:
                self._run_task(t)
            except Exception as e:
                with self._lock:
                    t.status = "FAILED"
                    t.errors.append(str(e))
                    t.reason_for_stop = f"crash: {e}"
                    t.finished = time.time()
                    t.updated = time.time()
                    self._save()
                self._notify_done(t)
            finally:
                # Released on every exit, including a crash. An orb left
                # showing work that has stopped is worse than one that never
                # showed it: the user waits for something that is not coming.
                #
                # With one worker, "no more QUEUED-and-ready tasks" was the
                # same question as "is anything still running" -- the one
                # worker finishing WAS the only thing that could have been
                # running. With a pool, that stopped being true: this
                # worker finishing task A while a sibling worker is still
                # running task B must not report "done" while B is still
                # going.
                with self._lock:
                    more = (any(x.status == "RUNNING" for x in self._tasks)
                           or any(x.status == "QUEUED" and self._deps_ready(x)
                                  for x in self._tasks))
                if not more:
                    self._signal_activity(False)

    def _run_task(self, t: Task) -> None:
        tool_exec = self._tool_executor
        if tool_exec is None:
            with self._lock:
                t.status = "FAILED"
                t.reason_for_stop = "no tool executor configured"
                t.finished = time.time()
            return
        n_steps = len(t.steps)
        for i, step in enumerate(t.steps):
            if t.status == "CANCELLED":
                break
            with self._lock:
                step.status = "RUNNING"
                step.started = time.time()
                t.current_step = i
                t.updated = time.time()
                self._save()
            # run the step (blocking is fine here — we're on the worker thread)
            try:
                result = tool_exec(step.tool, step.args, {})
                result = result if isinstance(result, str) else str(result)
                step.result = result[:4000]
            except Exception as e:
                step.result = ""
                step.error = str(e)[:500]
                step.status = "FAILED"
                step.verification = "FAILURE"
                step.finished = time.time()
                t.errors.append(f"step {i + 1} ({step.tool}): {e}")
                # attempt one retry with fresh args on transient-ish failures
                if step.retries < 1:
                    step.retries += 1
                    try:
                        result = tool_exec(step.tool, step.args, {})
                        step.result = (result if isinstance(result, str) else str(result))[:4000]
                        step.error = ""
                    except Exception as e2:
                        step.error = str(e2)[:500]
            finally:
                if step.status != "FAILED":
                    step.finished = time.time()
                    ver = self._verify_step(t, step)
                    step.verification = ver
                    if ver in ("CONFIRMED_SUCCESS", "LIKELY_SUCCESS"):
                        step.status = "VERIFIED"
                    elif ver == "FAILURE":
                        # A step whose output says it failed did not merely go
                        # unverified — it failed, and the task must stop and
                        # say so rather than finish as "could not be verified".
                        step.status = "FAILED"
                    else:
                        step.status = "UNVERIFIED"
                with self._lock:
                    self._validate_artifacts(t)
                    done = sum(1 for s in t.steps
                               if s.status in ("VERIFIED", "SKIPPED"))
                    t.progress = min(99, int(done / max(1, n_steps) * 100))
                    t.updated = time.time()
                    self._save()
            if step.status == "FAILED":
                break
        # final status decision — no false completion
        with self._lock:
            failed = [s for s in t.steps if s.status == "FAILED"]
            verified = [s for s in t.steps if s.status == "VERIFIED"]
            unverified = [s for s in t.steps if s.status == "UNVERIFIED"]
            skipped = [s for s in t.steps if s.status == "SKIPPED"]
            if t.status == "CANCELLED":
                pass
            elif failed:
                t.status = "FAILED"
                t.reason_for_stop = (f"step failed: {failed[0].tool} — {failed[0].error or failed[0].result[:120]}")
            elif unverified and verified:
                t.status = "PARTIALLY_COMPLETED"
                t.reason_for_stop = (f"{len(unverified)} step(s) not verified: "
                                     + ", ".join(s.tool for s in unverified))
            elif unverified:
                t.status = "UNVERIFIED"
                t.reason_for_stop = "steps ran but success could not be verified"
            elif verified:
                t.status = "COMPLETED"
                t.reason_for_stop = ""
            else:
                t.status = "UNVERIFIED"
                t.reason_for_stop = "no steps executed"
            t.progress = 100 if t.status == "COMPLETED" else t.progress
            t.finished = time.time()
            t.updated = time.time()
            self._save()
        self._remember_research(t)
        self._notify_done(t)

    #: Tools whose output is findings about the world, worth keeping so the
    #: same topic is not researched from scratch next time.
    RESEARCH_TOOLS = ("web_search",)

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
        msg = (f"Task '{t.title}' {t.status.lower().replace('_', ' ')}."
               if t.status == "COMPLETED" else
               f"Task '{t.title}' {t.status.lower().replace('_', ' ')}."
               + (f" {t.reason_for_stop}" if t.reason_for_stop else ""))
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
                mem.remember(outcome, source="system", confirmed=False, importance=0.55)
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
        try:
            from agent import planner as _planner
            try:
                plan = _planner.create_plan(goal)
            except Exception as e:
                log.warning("planning %r failed (%s); using the fallback plan", goal, e)
                plan = _planner._fallback_plan(goal)
        except Exception as e:
            log.warning("no planner available for %r: %s", goal, e)
            return []
        steps = []
        for st in (plan or {}).get("steps") or []:
            tool = st.get("tool")
            if tool:
                steps.append({"tool": tool, "args": dict(st.get("parameters") or st.get("args") or {})})
        return steps

    def exec_command(self, cmd: str, task_id: str = "", title: str = "",
                     steps: Optional[List[Dict[str, Any]]] = None,
                     meta: Optional[dict] = None,
                     estimated_duration_s: float = 0.0) -> str:
        c = (cmd or "").strip().lower()
        if c in ("create", "add", "submit", "run"):
            if not steps and (title or "").strip():
                # The model often names the goal and leaves the steps to us
                # ("research exoplanets"). Refusing that -- as this used to --
                # meant nothing was started while NOVA told the user it had
                # been. Plan it instead.
                steps = self.plan_steps(title.strip())
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
                n = sum(1 for t in self._tasks if self.cancel(t.id, force=True))
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