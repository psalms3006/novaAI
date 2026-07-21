"""
NOVA Connectivity Manager — Online/Degraded/Offline state machine with hysteresis.

Inspired by VYREN's runtime/connectivity.py. Provides graceful degradation
and offline task queuing.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("nova.connectivity")


class ConnectivityState(Enum):
    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"


@dataclass
class QueuedTask:
    """A task queued for later execution when connectivity returns."""
    description: str
    queued_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))
    requires_internet: bool = True


class ConnectivityManager:
    """
    Three-state connectivity manager with hysteresis to prevent flapping.

    States: ONLINE ←→ DEGRADED ←→ OFFLINE

    Transitions require consecutive failures/successes (hysteresis):
      - OFFLINE_THRESHOLD consecutive failures → OFFLINE
      - RECOVERY_THRESHOLD consecutive successes → ONLINE

    Publishes events via EventBus when state changes.
    """

    def __init__(
        self,
        event_bus: Optional[Any] = None,
        offline_threshold: int = 3,
        recovery_threshold: int = 2,
        offline_queue_path: Optional[str] = None,
    ) -> None:
        self._state = ConnectivityState.ONLINE
        self._event_bus = event_bus
        self._offline_threshold = offline_threshold
        self._recovery_threshold = recovery_threshold
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._lock = threading.Lock()
        self._queue: List[QueuedTask] = []
        self._queue_path = Path(offline_queue_path) if offline_queue_path else None
        self._callbacks: List[Callable[[ConnectivityState, ConnectivityState], None]] = []

        # Load existing queue from disk
        if self._queue_path and self._queue_path.exists():
            try:
                data = json.loads(self._queue_path.read_text(encoding="utf-8"))
                self._queue = [QueuedTask(**item) for item in data]
            except Exception as e:
                log.warning(f"Could not load offline queue: {e}")

    @property
    def state(self) -> ConnectivityState:
        with self._lock:
            return self._state

    @property
    def is_online(self) -> bool:
        return self.state in (ConnectivityState.ONLINE, ConnectivityState.DEGRADED)

    @property
    def is_fully_online(self) -> bool:
        return self.state == ConnectivityState.ONLINE

    def on_change(self, callback: Callable[[ConnectivityState, ConnectivityState], None]) -> None:
        """Register a callback for state transitions (old_state, new_state)."""
        self._callbacks.append(callback)

    def record_success(self) -> None:
        """Record a successful network operation."""
        with self._lock:
            old_state = self._state
            self._consecutive_failures = 0
            self._consecutive_successes += 1

            if self._state == ConnectivityState.OFFLINE:
                if self._consecutive_successes >= self._recovery_threshold:
                    self._state = ConnectivityState.DEGRADED
            elif self._state == ConnectivityState.DEGRADED:
                if self._consecutive_successes >= self._recovery_threshold:
                    self._state = ConnectivityState.ONLINE

        if self._state != old_state:
            self._notify(old_state, self._state)

    def record_failure(self) -> None:
        """Record a failed network operation."""
        with self._lock:
            old_state = self._state
            self._consecutive_successes = 0
            self._consecutive_failures += 1

            if self._state == ConnectivityState.ONLINE:
                if self._consecutive_failures >= 1:
                    self._state = ConnectivityState.DEGRADED
            elif self._state == ConnectivityState.DEGRADED:
                if self._consecutive_failures >= self._offline_threshold:
                    self._state = ConnectivityState.OFFLINE

        if self._state != old_state:
            self._notify(old_state, self._state)

    def queue_for_later(self, task_description: str, requires_internet: bool = True) -> str:
        """Queue a task for execution when connectivity returns."""
        task = QueuedTask(description=task_description, requires_internet=requires_internet)
        with self._lock:
            self._queue.append(task)
            self._persist_queue()
        return f"Task queued: '{task_description}' ({len(self._queue)} pending)"

    def get_queue(self) -> List[QueuedTask]:
        with self._lock:
            return list(self._queue)

    def clear_queue(self) -> int:
        with self._lock:
            count = len(self._queue)
            self._queue.clear()
            self._persist_queue()
            return count

    def _notify(self, old_state: ConnectivityState, new_state: ConnectivityState) -> None:
        """Notify callbacks and event bus of state change."""
        log.info(f"Connectivity: {old_state.value} → {new_state.value}")
        if self._event_bus:
            from core.event_bus import Events
            self._event_bus.publish_sync(
                Events.CONNECTIVITY_CHANGE,
                "connectivity_manager",
                {"old": old_state.value, "new": new_state.value},
            )
        for cb in self._callbacks:
            try:
                cb(old_state, new_state)
            except Exception as e:
                log.error(f"Connectivity callback error: {e}")

        # When coming back online, log queued tasks
        if new_state != ConnectivityState.OFFLINE and self._queue:
            log.info(f"Connectivity restored. {len(self._queue)} tasks queued for execution.")

    def _persist_queue(self) -> None:
        if not self._queue_path:
            return
        try:
            data = [
                {"description": t.description, "queued_at": t.queued_at, "requires_internet": t.requires_internet}
                for t in self._queue
            ]
            atomic_json_write(self._queue_path, data)
        except Exception as e:
            log.error(f"Failed to persist offline queue: {e}")

    def can_execute(self, requires_internet: bool = False) -> bool:
        """Check if a task requiring internet can be executed."""
        if not requires_internet:
            return True
        return self.is_fully_online

    def __repr__(self) -> str:
        return f"ConnectivityManager(state={self._state.value}, failures={self._consecutive_failures}, successes={self._consecutive_successes})"