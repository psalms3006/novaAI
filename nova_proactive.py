"""Deciding when NOVA may speak without being spoken to.

The route for unprompted speech already existed and was right:
`nova._start_ambient_intelligence` builds a `_speak` that hands text to the
live Gemini session when one is connected, and falls back to the transcript UI
otherwise. One session, never a second one.

What was missing was the judgement. `ProactiveAgent` was a stub whose `start()`
set a flag, so the log line "[DESK] proactive agent started" described an
assistant that could not say anything on its own initiative. Everything that
wants to reach the user unprompted -- a finished background task, a research
discovery, "should I remember that?", a warning about what is on screen --
comes through here, so there is one place that decides, and one voice.

The design is shaped by the failure mode on the other side. An assistant that
narrates its own progress is worse than one that stays quiet: "I'm still
researching", three times, is noise the user has to listen past. So low
priority events are recorded and never spoken, ordinary findings are spaced
out, and only genuine urgency is allowed to bypass the spacing.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Optional

log = logging.getLogger("nova.proactive")

__all__ = ["ProactiveAgent", "ProactiveEvent", "Priority"]


class Priority:
    """How much this is worth interrupting for."""
    CRITICAL = "critical"   # a safety warning; always, immediately
    HIGH = "high"           # completion, an error, a question NOVA needs answered
    MEDIUM = "medium"       # a discovery or milestone worth knowing about
    LOW = "low"             # routine progress; recorded, never spoken

    _RANK = {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3}

    @classmethod
    def rank(cls, priority: str) -> int:
        return cls._RANK.get(priority, 3)


@dataclass
class ProactiveEvent:
    """Something a subsystem believes the user may want to hear."""
    kind: str                       # safety|completion|needs_input|error|discovery|milestone|progress
    message: str
    priority: str = Priority.MEDIUM
    requires_response: bool = False
    task_id: str = ""
    #: Identity of the *thing*, not the wording. Two reports of the same
    #: finding should not be announced twice even if phrased differently.
    dedupe_key: str = ""
    created: float = field(default_factory=time.time)


class ProactiveAgent:
    """The one place that decides whether the user gets interrupted."""

    #: Ordinary findings are spaced at least this far apart.
    MIN_GAP_S = 300.0
    #: How long a given finding stays "already said".
    DEDUPE_WINDOW_S = 3600.0
    #: An unattended agent must not accumulate messages forever. Oldest
    #: low-priority items are dropped first.
    MAX_QUEUE = 32

    def __init__(self, speak_fn: Optional[Callable[[str], None]] = None,
                 meta: Optional[dict] = None, planner=None) -> None:
        self.speak_fn = speak_fn
        self.meta = meta or {}
        self.planner = planner
        self._queue: Deque[ProactiveEvent] = deque()
        self._recent: dict[str, float] = {}
        self._last_spoke_at = 0.0
        self._awaiting_response = False
        self._busy_fn: Optional[Callable[[], bool]] = None
        self._lock = threading.RLock()
        self._running = False
        self._now = time.time          # seam: tests freeze and advance this

    # ── wiring ──────────────────────────────────────────────────────────────
    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def update_speak(self, speak_fn: Optional[Callable[[str], None]]) -> None:
        self.speak_fn = speak_fn

    def set_busy(self, busy_fn: Optional[Callable[[], bool]]) -> None:
        """Tell the agent how to find out whether NOVA is mid-sentence.

        Wired to the live session's `speaking` flag. Talking over the answer
        the user actually asked for is the rudest thing this class could do.
        """
        self._busy_fn = busy_fn

    def awaiting_response(self) -> bool:
        return self._awaiting_response

    def answered(self) -> None:
        """The user replied to whatever NOVA asked."""
        self._awaiting_response = False

    # ── the decision ────────────────────────────────────────────────────────
    def emit(self, event: ProactiveEvent) -> bool:
        """Offer something to the user. True if it was spoken now.

        Never raises: callers are background worker threads, and a failure to
        announce must not take down the work being announced.
        """
        try:
            with self._lock:
                return self._consider(event)
        except Exception:
            log.exception("[PROACTIVE] failed to handle %s", event.kind)
            return False

    def _consider(self, event: ProactiveEvent) -> bool:
        if not (event.message or "").strip():
            return False

        if Priority.rank(event.priority) >= Priority.rank(Priority.LOW):
            # Routine progress. Worth a log line, never worth a sentence.
            log.info("[PROACTIVE] (silent) %s: %s", event.kind, event.message)
            return False

        if self._is_duplicate(event):
            log.info("[PROACTIVE] suppressed duplicate: %s", event.dedupe_key)
            return False

        if self._busy():
            self._enqueue(event)
            return False

        if event.priority != Priority.CRITICAL and not self._gap_elapsed(event):
            self._enqueue(event)
            return False

        return self._say(event)

    def _is_duplicate(self, event: ProactiveEvent) -> bool:
        key = event.dedupe_key
        if not key:
            return False
        said_at = self._recent.get(key)
        return said_at is not None and (self._now() - said_at) < self.DEDUPE_WINDOW_S

    def _gap_elapsed(self, event: ProactiveEvent) -> bool:
        """HIGH always passes; MEDIUM waits its turn."""
        if Priority.rank(event.priority) <= Priority.rank(Priority.HIGH):
            return True
        return (self._now() - self._last_spoke_at) >= self.MIN_GAP_S

    def _busy(self) -> bool:
        if self._busy_fn is None:
            return False
        try:
            return bool(self._busy_fn())
        except Exception:
            return False          # if we cannot tell, do not go mute forever

    def _say(self, event: ProactiveEvent) -> bool:
        if self.speak_fn is None:
            log.info("[PROACTIVE] (no voice) %s", event.message)
            return False
        try:
            self.speak_fn(event.message)
        except Exception:
            # A dead speaker must not propagate into the worker thread that
            # was merely reporting its result.
            log.exception("[PROACTIVE] speak failed")
            return False

        self._last_spoke_at = self._now()
        if event.dedupe_key:
            self._recent[event.dedupe_key] = self._now()
        if event.requires_response:
            self._awaiting_response = True
        log.info("[PROACTIVE] spoke (%s/%s): %s",
                 event.kind, event.priority, event.message[:80])
        return True

    # ── holding and releasing ───────────────────────────────────────────────
    def _enqueue(self, event: ProactiveEvent) -> None:
        if event.dedupe_key and any(
            q.dedupe_key == event.dedupe_key for q in self._queue
        ):
            return
        self._queue.append(event)
        while len(self._queue) > self.MAX_QUEUE:
            self._drop_least_important()

    def _drop_least_important(self) -> None:
        """Make room. The least urgent, oldest thing goes."""
        victim = max(
            range(len(self._queue)),
            key=lambda i: (Priority.rank(self._queue[i].priority),
                           -self._queue[i].created),
        )
        dropped = self._queue[victim]
        del self._queue[victim]
        log.info("[PROACTIVE] queue full; dropped %s: %s",
                 dropped.kind, dropped.message[:60])

    def flush(self) -> int:
        """Say what has been waiting, most urgent first. Returns how many."""
        spoken = 0
        with self._lock:
            if self._busy():
                return 0
            pending = sorted(self._queue, key=lambda e: (
                Priority.rank(e.priority), e.created))
            self._queue.clear()
            for event in pending:
                if self._busy():
                    self._queue.append(event)
                    continue
                if self._is_duplicate(event):
                    continue
                if self._say(event):
                    spoken += 1
                else:
                    self._queue.append(event)
        return spoken

    def pending(self) -> int:
        with self._lock:
            return len(self._queue)
