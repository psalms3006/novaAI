"""Workflows that outlive the conversation that created them.

"Post twice a day for the next month" cannot live in a timer inside the
process that heard the request. NOVA gets closed, the machine reboots, and a
month-long commitment evaporates without anyone being told. So a workflow is a
persisted record and its next run is a stored timestamp, compared against the
clock rather than counted down in memory. Restarting NOVA re-reads the file
and carries on; nothing is rescheduled from "now" just because the process is
new.

The judgement that needed the most care is what to do about runs that came due
while NOVA was not running. Executing every missed run the moment she starts
is how an assistant publishes eleven posts in one minute, each of them stale.
Lateness is therefore graded rather than binary:

    within the grace window   run it; ten minutes late is still on time
    beyond the stale window   skip this one, count it, move to the next slot
    past the workflow's end   the workflow is over, not merely late

`nova_heartbeat.HeartbeatState` already applies this discipline to NOVA's
internal health checks -- storing when each check is next due so a restart
does not reset it. This is the same idea for work the user asked for.

The scheduler decides *when*; it never decides *what*. Running a workflow is
delegated to an injected runner, so this module has no idea what a social post
or an inbox sweep is, and the connectors in `integrations/` cannot make the
schedule behave differently by existing.
"""
from __future__ import annotations

import enum
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import nova_paths

log = logging.getLogger("nova.scheduler")

__all__ = [
    "Scheduler", "Workflow", "WorkflowStatus", "ApprovalMode", "RunOutcome",
    "WorkflowRunner", "get_scheduler",
]

WORKFLOWS_FILENAME = "workflows.json"

#: A run this late is still worth doing as intended.
GRACE_SECONDS = 30 * 60
#: Past this, the moment has gone: skip it rather than publish something the
#: user would not recognise as current.
STALE_SECONDS = 6 * 3600
#: Consecutive failures before a workflow stops trying and says so.
MAX_FAILURES = 5
#: Retry backoff, capped so a broken connector cannot push the next attempt
#: years into the future.
RETRY_BASE_SECONDS = 5 * 60
RETRY_CAP_SECONDS = 6 * 3600
#: The tightest cadence a workflow may actually run at. See _next_slot.
MIN_INTERVAL_SECONDS = 60.0


class WorkflowStatus(str, enum.Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"


class ApprovalMode(str, enum.Enum):
    """How much the user wants to be involved in each run."""
    FULL_AUTO = "full_auto"
    APPROVE_BEFORE_PUBLISH = "approve_before_publish"
    DRAFT_ONLY = "draft_only"
    PAUSED = "paused"


class RunOutcome(str, enum.Enum):
    DONE = "done"
    FAILED = "failed"
    #: The runner decided there was nothing worth doing this time. Not a
    #: failure: an assistant allowed to skip a post is one that does not
    #: manufacture filler to satisfy a schedule.
    NOTHING_TO_DO = "nothing_to_do"


@dataclass
class Workflow:
    title: str
    kind: str
    every_seconds: float
    starts_at: float
    ends_at: Optional[float] = None
    approval: ApprovalMode = ApprovalMode.APPROVE_BEFORE_PUBLISH
    status: WorkflowStatus = WorkflowStatus.ACTIVE
    id: str = field(default_factory=lambda: f"W-{uuid.uuid4().hex[:8]}")
    next_due: float = 0.0
    last_run: float = 0.0
    runs: int = 0
    missed: int = 0
    failures: int = 0
    params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.next_due:
            self.next_due = self.starts_at

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["approval"] = ApprovalMode(self.approval).value
        data["status"] = WorkflowStatus(self.status).value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Workflow":
        known = {k: v for k, v in data.items()
                 if k in cls.__dataclass_fields__}
        known["approval"] = ApprovalMode(known.get("approval",
                                                   ApprovalMode.APPROVE_BEFORE_PUBLISH))
        known["status"] = WorkflowStatus(known.get("status",
                                                   WorkflowStatus.ACTIVE))
        return cls(**known)

    def describe(self) -> str:
        """One line, for saying out loud."""
        when = time.strftime("%a %d %b %H:%M", time.localtime(self.next_due))
        every_h = self.every_seconds / 3600.0
        return (f"{self.title} — every {every_h:.0f}h, {self.status.value}, "
                f"next {when}")



class WorkflowRunner:
    """Turns a due workflow into the work it stands for.

    A registry rather than a chain of ifs, so a connector adds a kind without
    the scheduler learning anything about it. Kinds with no handler are the
    interesting case: there is no TikTok connector, so a TikTok workflow must
    say so rather than quietly succeed at nothing -- a workflow that runs
    forever doing nothing is worse than one that stops and explains itself.

    Results are offered to the proactive agent, which owns the decision about
    whether the user is interrupted. Routine runs stay silent.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[["Workflow"], "RunOutcome"]] = {}
        self._unhandled: set[str] = set()
        self._proactive = None

    def register(self, kind: str,
                 handler: Callable[["Workflow"], "RunOutcome"]) -> None:
        self._handlers[kind] = handler

    def handles(self, kind: str) -> bool:
        return kind in self._handlers

    def unhandled_kinds(self) -> set[str]:
        """Kinds asked for that nothing can do. Worth reporting to the user."""
        return set(self._unhandled)

    def set_proactive(self, agent) -> None:
        self._proactive = agent

    def __call__(self, workflow: "Workflow") -> "RunOutcome":
        handler = self._handlers.get(workflow.kind)
        if handler is None:
            self._unhandled.add(workflow.kind)
            log.warning("[SCHED] no handler for kind %r (%s)",
                        workflow.kind, workflow.id)
            return RunOutcome.FAILED

        outcome = RunOutcome(handler(workflow))
        self._announce(workflow, outcome)
        return outcome

    def _announce(self, workflow: "Workflow", outcome: "RunOutcome") -> None:
        """Offer the result. The proactive policy decides if it is spoken."""
        if self._proactive is None or outcome is RunOutcome.NOTHING_TO_DO:
            return
        try:
            from nova_proactive import Priority, ProactiveEvent
            self._proactive.emit(ProactiveEvent(
                kind="completion",
                priority=Priority.MEDIUM,
                message=f"{workflow.title} is done.",
                dedupe_key=f"{workflow.id}:{workflow.runs}",
            ))
        except Exception:
            log.debug("[SCHED] could not announce %s", workflow.id, exc_info=True)


class Scheduler:
    """Decides which workflows are due, and what to do about late ones."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = Path(path) if path else nova_paths.data_file(WORKFLOWS_FILENAME)
        self._lock = threading.RLock()
        self._workflows: dict[str, Workflow] = {}
        self._clock: Callable[[], float] = time.time
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._load()

    # ── seams ───────────────────────────────────────────────────────────────
    def set_clock(self, clock: Callable[[], float]) -> None:
        """Tests drive time; nothing here should wait on a real month."""
        self._clock = clock

    def now(self) -> float:
        return self._clock()

    # ── persistence ─────────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                for item in raw or []:
                    try:
                        w = Workflow.from_dict(item)
                        self._workflows[w.id] = w
                    except Exception:
                        log.warning("[SCHED] dropping unreadable workflow",
                                    exc_info=True)
        except Exception:
            log.warning("[SCHED] could not read %s; starting empty", self.path)

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps([w.to_dict() for w in self._workflows.values()],
                           indent=2),
                encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            log.warning("[SCHED] could not write %s", self.path, exc_info=True)

    # ── the register ────────────────────────────────────────────────────────
    def add(self, workflow: Workflow) -> Workflow:
        with self._lock:
            if not workflow.next_due:
                workflow.next_due = max(workflow.starts_at, self.now())
            self._workflows[workflow.id] = workflow
            self._save()
        log.info("[SCHED] added %s", workflow.describe())
        return workflow

    def get(self, workflow_id: str) -> Optional[Workflow]:
        with self._lock:
            return self._workflows.get(workflow_id)

    def workflows(self) -> list[Workflow]:
        with self._lock:
            return list(self._workflows.values())

    # ── user control ────────────────────────────────────────────────────────
    def pause(self, workflow_id: str) -> bool:
        return self._set_status(workflow_id, WorkflowStatus.PAUSED)

    def resume(self, workflow_id: str) -> bool:
        with self._lock:
            w = self._workflows.get(workflow_id)
            if w is None or w.status in (WorkflowStatus.CANCELLED,
                                         WorkflowStatus.EXPIRED):
                return False
            w.status = WorkflowStatus.ACTIVE
            w.failures = 0
            # Do not fire immediately for everything missed while paused.
            if w.next_due < self.now():
                w.next_due = self.now()
            self._save()
        return True

    def cancel(self, workflow_id: str) -> bool:
        return self._set_status(workflow_id, WorkflowStatus.CANCELLED)

    def reschedule(self, workflow_id: str, every_seconds: Optional[float] = None,
                   ends_at: Optional[float] = None,
                   approval: Optional[ApprovalMode] = None) -> bool:
        with self._lock:
            w = self._workflows.get(workflow_id)
            if w is None:
                return False
            if every_seconds is not None:
                w.every_seconds = float(every_seconds)
                w.next_due = min(w.next_due, self.now() + w.every_seconds)
            if ends_at is not None:
                w.ends_at = ends_at
            if approval is not None:
                w.approval = ApprovalMode(approval)
            self._save()
        return True

    def _set_status(self, workflow_id: str, status: WorkflowStatus) -> bool:
        with self._lock:
            w = self._workflows.get(workflow_id)
            if w is None:
                return False
            w.status = status
            self._save()
        return True

    # ── due-ness ────────────────────────────────────────────────────────────
    def due(self) -> list[Workflow]:
        now = self.now()
        with self._lock:
            out = []
            for w in self._workflows.values():
                if w.status is not WorkflowStatus.ACTIVE:
                    continue
                if w.approval is ApprovalMode.PAUSED:
                    continue
                if w.ends_at is not None and now > w.ends_at:
                    continue        # expired; run_due retires it
                if w.next_due <= now:
                    out.append(w)
            return out

    def run_due(self, runner: Callable[[Workflow], RunOutcome]) -> dict[str, int]:
        """Execute whatever is due. Returns a tally.

        `runner` is given the workflow and decides what running it means --
        including whether its approval mode permits publishing. The scheduler
        owns *when*, never *what*.
        """
        tally = {"ran": 0, "skipped": 0, "failed": 0, "expired": 0,
                 "nothing_to_do": 0}
        now = self.now()

        with self._lock:
            candidates = list(self._workflows.values())

        for w in candidates:
            if w.status is not WorkflowStatus.ACTIVE:
                continue
            if w.approval is ApprovalMode.PAUSED:
                continue

            if w.ends_at is not None and now > w.ends_at:
                w.status = WorkflowStatus.EXPIRED
                tally["expired"] += 1
                log.info("[SCHED] %s expired", w.id)
                continue

            if w.next_due > now:
                continue

            lateness = now - w.next_due
            if lateness > STALE_SECONDS:
                # The moment has gone. Count it and move to the next slot
                # rather than publishing something stale as if it were fresh.
                w.missed += 1
                w.next_due = self._next_slot(w, now)
                tally["skipped"] += 1
                log.info("[SCHED] %s skipped a run %.1fh late",
                         w.id, lateness / 3600.0)
                continue

            try:
                outcome = RunOutcome(runner(w))
            except Exception:
                log.warning("[SCHED] runner raised for %s", w.id, exc_info=True)
                outcome = RunOutcome.FAILED

            if outcome is RunOutcome.FAILED:
                w.failures += 1
                tally["failed"] += 1
                if w.failures >= MAX_FAILURES:
                    w.status = WorkflowStatus.FAILED
                    log.warning("[SCHED] %s stopped after %d failures",
                                w.id, w.failures)
                else:
                    backoff = min(RETRY_BASE_SECONDS * (2 ** (w.failures - 1)),
                                  RETRY_CAP_SECONDS)
                    w.next_due = now + backoff
            else:
                w.failures = 0
                w.last_run = now
                w.runs += 1
                w.next_due = self._next_slot(w, now)
                if outcome is RunOutcome.NOTHING_TO_DO:
                    tally["nothing_to_do"] += 1
                else:
                    tally["ran"] += 1

        with self._lock:
            self._save()
        return tally

    def _next_slot(self, w: Workflow, now: float) -> float:
        """The next future slot on this workflow's cadence.

        Advancing by one interval from a long-stale due time would leave the
        workflow still in the past, so it would fire again immediately and
        keep firing until it caught up -- the burst this design exists to
        avoid.
        """
        # A floor, deliberately. These workflows call other people's APIs,
        # and a cadence of seconds -- from a typo, or a model inventing
        # "every_seconds": 1 -- would hammer a provider until it rate-limited
        # the user's account. Sub-minute scheduling is not a thing this is
        # for, so it is clamped rather than honoured.
        step = max(MIN_INTERVAL_SECONDS, float(w.every_seconds))
        nxt = w.next_due + step
        if nxt <= now:
            missed_steps = int((now - w.next_due) // step) + 1
            nxt = w.next_due + missed_steps * step
        return nxt


    # ── the loop ────────────────────────────────────────────────────────────
    def start(self, runner: Callable[["Workflow"], "RunOutcome"],
              interval_seconds: float = 60.0) -> None:
        """Tick until stopped. Idempotent.

        A daemon thread, not a per-workflow timer: the schedule lives in the
        file, and this only asks "is anything due yet". That is what lets a
        month-long workflow survive a restart -- there is no countdown to
        lose.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, args=(runner, float(interval_seconds)),
                daemon=True, name="NOVAScheduler")
            self._thread.start()
        log.info("[SCHED] loop started (every %.0fs)", interval_seconds)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None

    def _loop(self, runner, interval_seconds: float) -> None:
        while not self._stop.is_set():
            try:
                self.run_due(runner)
            except Exception:
                # One bad tick must not end the loop: the workflow that broke
                # will back off on its own, and the others are still due.
                log.warning("[SCHED] tick failed", exc_info=True)
            self._stop.wait(interval_seconds)

    # ── narration ───────────────────────────────────────────────────────────
    def summary(self) -> str:
        """What NOVA says when asked what she has scheduled."""
        with self._lock:
            items = [w for w in self._workflows.values()
                     if w.status in (WorkflowStatus.ACTIVE, WorkflowStatus.PAUSED)]
        if not items:
            return "Nothing is scheduled."
        return "\n".join(w.describe() for w in items)


_scheduler: Optional[Scheduler] = None


def get_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
    return _scheduler
