"""
NOVA Event Bus — Thread-safe pub/sub with pattern matching and priority dispatch.

Inspired by VYREN's event_bus.py. Provides loose coupling between subsystems.
"""
from __future__ import annotations

import fnmatch
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("nova.events")


@dataclass
class Event:
    """An event published to the bus."""
    type: str
    source: str
    data: dict = field(default_factory=dict)
    priority: int = 5  # 1=highest, 10=lowest
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    timestamp: str = field(default_factory=lambda: time.strftime("%H:%M:%S"))

    def __lt__(self, other: Event) -> bool:
        return self.priority < other.priority


# Type alias for event handlers
EventHandler = Callable[[Event], None]


class EventBus:
    """
    Thread-safe publish/subscribe event bus with glob pattern matching.

    Events are dispatched by specificity:
      1. Exact match (priority 3)
      2. Glob pattern match (priority 2)
      3. Wildcard "*" match (priority 1)

    Usage:
        bus = EventBus()
        bus.subscribe("memory.*", my_handler)
        bus.subscribe("memory.stored", specific_handler)
        bus.publish(Event(type="memory.stored", source="faiss", data={"fact": "..."}))
    """

    def __init__(self, max_history: int = 1000) -> None:
        self._handlers: Dict[str, List[Tuple[int, EventHandler]]] = {}  # pattern -> [(specificity, handler)]
        self._history: List[Event] = []
        self._max_history = max_history
        self._lock = threading.Lock()

    def subscribe(self, pattern: str, handler: EventHandler) -> None:
        """Subscribe a handler to an event pattern (supports glob wildcards)."""
        with self._lock:
            if pattern not in self._handlers:
                self._handlers[pattern] = []
            self._handlers[pattern].append(handler)

    def unsubscribe(self, pattern: str, handler: EventHandler) -> None:
        """Remove a handler from a pattern."""
        with self._lock:
            if pattern in self._handlers:
                self._handlers[pattern] = [
                    h for h in self._handlers[pattern] if h != handler
                ]

    def publish(self, event: Event) -> List[Exception]:
        """
        Publish an event to all matching handlers.
        Returns list of any exceptions raised by handlers (never crashes).
        """
        handlers_to_call = self._resolve_handlers(event.type)
        errors = []

        for handler in handlers_to_call:
            try:
                handler(event)
            except Exception as e:
                log.error(f"Event handler error for '{event.type}': {e}")
                errors.append(e)

        # Record in history
        with self._lock:
            self._history.append(event)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]

        return errors

    def publish_sync(self, event_type: str, source: str, data: dict = None, priority: int = 5) -> List[Exception]:
        """Convenience: publish a simple event by type string."""
        return self.publish(Event(
            type=event_type,
            source=source,
            data=data or {},
            priority=priority,
        ))

    def _resolve_handlers(self, event_type: str) -> List[EventHandler]:
        """Resolve all handlers that match the event type, ordered by specificity."""
        with self._lock:
            results: List[Tuple[int, EventHandler]] = []

            for pattern, handlers in self._handlers.items():
                if pattern == event_type:
                    # Exact match — highest specificity
                    for h in handlers:
                        results.append((3, h))
                elif pattern == "*":
                    # Wildcard — lowest specificity
                    for h in handlers:
                        results.append((1, h))
                elif fnmatch.fnmatch(event_type, pattern):
                    # Glob match — medium specificity
                    for h in handlers:
                        results.append((2, h))

            # Sort by specificity descending (exact first, then glob, then wildcard)
            results.sort(key=lambda x: -x[0])
            return [h for _, h in results]

    def get_history(self, event_type: str = None, limit: int = 50) -> List[Event]:
        """Get recent events, optionally filtered by type."""
        with self._lock:
            events = self._history
            if event_type:
                events = [e for e in events if e.type == event_type]
            return events[-limit:]

    def clear_history(self) -> None:
        """Clear all stored events."""
        with self._lock:
            self._history.clear()


# Predefined event types (NOVA domain)
class Events:
    """Predefined event type constants for NOVA."""
    # Memory events
    MEMORY_STORED = "memory.stored"
    MEMORY_SEARCHED = "memory.searched"
    MEMORY_DELETED = "memory.deleted"
    MEMORY_CONSOLIDATED = "memory.consolidated"

    # Knowledge graph events
    KG_ENTITY_ADDED = "kg.entity_added"
    KG_RELATION_ADDED = "kg.relation_added"
    KG_QUERY = "kg.query"

    # System events
    SYSTEM_STARTUP = "system.startup"
    SYSTEM_SHUTDOWN = "system.shutdown"
    SYSTEM_ERROR = "system.error"

    # Connectivity
    CONNECTIVITY_CHANGE = "connectivity.change"
    CONNECTIVITY_ONLINE = "connectivity.online"
    CONNECTIVITY_OFFLINE = "connectivity.offline"

    # Tool events
    TOOL_CALLED = "tool.called"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"

    # Agent events
    AGENT_SPAWNED = "agent.spawned"
    AGENT_COMPLETED = "agent.completed"
    AGENT_FAILED = "agent.failed"

    # Heartbeat
    HEARTBEAT_TICK = "heartbeat.tick"
    HEARTBEAT_NOTICE = "heartbeat.notice"

    # Voice
    VOICE_WAKE = "voice.wake"
    VOICE_SPEAK = "voice.speak"
    VOICE_LISTEN = "voice.listen"

    # Safety
    SAFETY_BLOCKED = "safety.blocked"
    SAFETY_CONFIRMED = "safety.confirmed"