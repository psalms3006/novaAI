"""
agent_activity.py
═════════════════
The one place that knows which of NOVA's agents is working right now.

Three things run tools: the voice session, the typed-chat path and the
background task manager. Each used to tell the window about agents in its own
way -- or not at all: a research task could run for minutes while the Agents
panel said RESEARCH standby, because only the chat path wrote agent state, and
it keyed that state so loosely that rows it lit were never switched off.

Now every executor reports here -- ``begin()`` when an agent starts on
something, ``end()`` when it stops -- and ``/api/system`` reads ``snapshot()``.
An agent is "running" exactly while at least one piece of real work it owns is
open, and says what that work is.

Import-safe and dependency-free: the task manager, the live session and the
bridge all import it.
"""
from __future__ import annotations

import itertools
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("nova.agents")

__all__ = ["AGENTS", "agent_for_tool", "begin", "end", "snapshot", "set_observer", "reset",
           "resource_for_tool", "resource_lock"]

#: The roster, in display order: (id, label, what it does).
AGENTS = [
    ("orchestrator", "NOVA", "Manager"),
    ("research", "RESEARCH", "Web + sources"),
    ("browser", "BROWSER", "Browser control"),
    ("computer", "COMPUTER", "Apps, files + desktop"),
    ("creative", "CREATIVE", "Writing + documents"),
    ("reviewer", "REVIEWER", "Checks finished work"),
    ("code", "CODE", "Code + analysis"),
    ("vision", "VISION", "Screen + images"),
    ("memory", "MEMORY", "Recall + storage"),
    ("meeting", "MEETING", "Meetings"),
    ("surveillance", "SURVEIL", "Monitoring"),
    ("spawn", "SPAWN", "Sub-agents"),
]
AGENT_IDS = [a[0] for a in AGENTS]

#: Exact tool names first -- a substring match sent generate_document to
#: whichever pattern happened to come first.
_EXACT = {
    "web_search": "research", "learn_resource": "research",
    "browser_control": "browser",
    "computer_control": "computer", "app_control": "computer", "open_app": "computer",
    "close_app": "computer", "computer_settings": "computer", "file_controller": "computer",
    "autostart": "computer",
    "generate_document": "creative",
    "file_processor": "code", "self_editor": "code",
    "vision": "vision",
    "remember_fact": "memory", "nova_memory": "memory",
    "nova_task": "orchestrator", "planner": "orchestrator",
    "review": "reviewer",
}
_LOOSE = [
    ("research", ("search", "research")),
    ("browser", ("browser",)),
    ("vision", ("vision", "screen", "ocr")),
    ("creative", ("document", "write", "creative")),
    ("code", ("code",)),
    ("memory", ("memory", "remember")),
    ("computer", ("app", "file", "desktop", "settings")),
    ("meeting", ("meeting",)),
    ("surveillance", ("surveil", "monitor")),
    ("spawn", ("spawn",)),
]


def agent_for_tool(tool: str) -> str:
    """The agent that owns *tool*. Unknown tools belong to NOVA herself."""
    name = (tool or "").strip().lower()
    if name in _EXACT:
        return _EXACT[name]
    if name in AGENT_IDS:
        return name
    for agent, patterns in _LOOSE:
        if any(p in name for p in patterns):
            return agent
    return "orchestrator"


_lock = threading.Lock()
_open: Dict[str, Dict[str, Any]] = {}
_counter = itertools.count(1)
_observer: Optional[Callable[[Dict[str, Any]], None]] = None


def set_observer(fn: Optional[Callable[[Dict[str, Any]], None]]) -> None:
    """Called with an ``agent.state`` event whenever an agent starts or stops."""
    global _observer
    _observer = fn


def _emit(agent: str) -> None:
    fn = _observer
    if fn is None:
        return
    try:
        fn({"type": "agent.state", "agent": agent, **_row(agent), "ts": time.time()})
    except Exception:
        log.debug("agent observer failed", exc_info=True)


def _row(agent: str) -> Dict[str, Any]:
    with _lock:
        work = sorted((w for w in _open.values() if w["agent"] == agent),
                      key=lambda w: w["since"])
    if not work:
        return {"state": "standby", "action": "", "task_id": "", "work": []}
    latest = work[-1]
    return {
        "state": "running",
        "action": latest["action"],
        "task_id": latest["task_id"],
        "since": work[0]["since"],
        "work": [dict(w) for w in work],
    }


def begin(agent: str, action: str, *, source: str, task_id: str = "", key: str = "") -> str:
    """Mark *agent* as working on *action*. Returns the token ``end()`` takes.

    *key* lets a caller that cannot hold the token (the chat path names its
    agents "agent-web_search-<turn>") end the work by the same name.
    """
    agent = agent if agent in AGENT_IDS else agent_for_tool(agent)
    token = key or f"w{next(_counter)}"
    with _lock:
        _open[token] = {"agent": agent, "action": (action or "")[:160], "source": source,
                        "task_id": task_id or "", "since": time.time()}
    _emit(agent)
    return token


def end(token: str) -> None:
    """Close the work *token* opened. Unknown or repeated tokens are ignored."""
    with _lock:
        work = _open.pop(token, None)
    if work is not None:
        _emit(work["agent"])


def snapshot() -> List[Dict[str, Any]]:
    """Every agent in roster order, with its real current state."""
    return [{"id": aid, "label": label, "role": role, **_row(aid)}
            for aid, label, role in AGENTS]


def reset() -> None:
    """Forget all open work (tests)."""
    with _lock:
        _open.clear()


# ── shared resources ─────────────────────────────────────────────────────────
#: Tools that drive something only one piece of work can use at a time. Two
#: tasks moving the mouse, or typing into the same browser, ruin each other's
#: work; the voice session doing it under a running task does the same.
_RESOURCES = {
    "desktop": ("computer_control", "app_control", "open_app", "close_app",
                "computer_settings"),
    "browser": ("browser_control",),
}
_resource_locks: Dict[str, threading.RLock] = {name: threading.RLock() for name in _RESOURCES}


def resource_for_tool(tool: str) -> str:
    """The exclusive resource *tool* needs, or "" when it needs none."""
    for name, tools in _RESOURCES.items():
        if tool in tools:
            return name
    return ""


def resource_lock(name: str) -> threading.RLock:
    """Re-entrant, so a task step holding it can call the tool dispatcher,
    which takes it again on the same thread."""
    return _resource_locks[name]
