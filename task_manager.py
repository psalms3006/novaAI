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
import json, os, re, threading, time, uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

__all__ = ["TaskManager", "TaskStep", "get_task_manager", "init_task_manager"]

TASK_STATUSES = ["PLANNED", "QUEUED", "RUNNING", "WAITING", "VERIFYING",
                 "COMPLETED", "FAILED", "CANCELLED", "PARTIALLY_COMPLETED",
                 "UNVERIFIED"]
STEP_STATUSES = ["QUEUED", "RUNNING", "VERIFIED", "UNVERIFIED", "FAILED",
                 "SKIPPED"]

DEFAULT_TASKS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "nova_tasks_aios.json"
)

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
            "progress": self.progress, "current_step": self.current_step,
            "created": self.created, "updated": self.updated,
            "started": self.started, "finished": self.finished,
            "errors": list(self.errors), "reason_for_stop": self.reason_for_stop,
            "notify_done": self.notify_done,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Task":
        steps = [TaskStep.from_dict(s) for s in d.get("steps", [])]
        t = cls(title=d.get("title", "Task"), steps=steps)
        for k, v in d.items():
            if k == "steps":
                continue
            if hasattr(t, k):
                setattr(t, k, v)
        return t

    def summary(self, detail: bool = False) -> str:
        done = sum(1 for s in self.steps if s.status in ("VERIFIED", "SKIPPED"))
        total = len(self.steps)
        lines = [f"{self.id} · {self.title} · {self.status} ({self.progress}%)"]
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

    def __init__(self, path: str = DEFAULT_TASKS_PATH,
                 tool_executor: Optional[Callable[[str, Dict, Dict], Any]] = None,
                 verify_fn: Optional[Callable[[str], str]] = None) -> None:
        self.path = path
        self._tool_executor = tool_executor      # fn(tool_name, args, meta) -> result str
        self._verify_fn = verify_fn              # fn(output_text) -> verification status
        self._notify: Optional[Callable[[str], None]] = None
        self._speak = None
        self._lock = threading.RLock()
        self._tasks: List[Task] = []
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._cond = threading.Condition(self._lock)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._load()

    # ── wiring ──────────────────────────────────────────────────────────────
    def set_notify(self, fn: Optional[Callable[[str], None]]) -> None:
        self._notify = fn

    def set_tool_executor(self, fn: Callable[[str, Dict, Dict], Any]) -> None:
        self._tool_executor = fn

    def set_verify(self, fn: Callable[[str], str]) -> None:
        self._verify_fn = fn

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._worker_loop, daemon=True,
                                            name="NOVATaskManager")
            self._thread.start()

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
               task_id: Optional[str] = None) -> Task:
        """steps: list of {"tool": ..., "args": {...}}. Returns the created Task."""
        if not steps:
            steps = [{"tool": "agent", "args": {"objective": title}}]
        with self._lock:
            t = Task(title=title, steps=[TaskStep(tool=s["tool"], args=s.get("args", {}))
                                        for s in steps],
                     dependencies=list(dependencies or []), priority=priority.upper())
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
        failure = ["error:", "failed", "exception", "not found", "unavailable",
                   "denied", "blocked", "timeout", "unable to", "unknown "]
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
                    step.status = "VERIFIED" if ver in ("CONFIRMED_SUCCESS", "LIKELY_SUCCESS") \
                        else "UNVERIFIED"
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
        self._notify_done(t)

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
    def exec_command(self, cmd: str, task_id: str = "", title: str = "",
                     steps: Optional[List[Dict[str, Any]]] = None,
                     meta: Optional[dict] = None) -> str:
        c = (cmd or "").strip().lower()
        if c in ("create", "add", "submit", "run"):
            t = self.submit(title or "Untitled task", steps or [], meta=meta)
            return (f"Task {t.id} queued: '{t.title}' "
                    f"({len(t.steps)} step(s)). You'll be notified when it finishes.")
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
                      auto_start: bool = True) -> TaskManager:
    global _singleton
    _singleton = TaskManager(path=path if path else DEFAULT_TASKS_PATH,
                             tool_executor=tool_executor,
                             verify_fn=verify_fn)
    if auto_start:
        _singleton.start()
    return _singleton


def get_task_manager() -> Optional[TaskManager]:
    return _singleton