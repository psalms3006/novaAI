"""desk.trace — per-request stage timing for the chat pipeline.

Assigns one request id to a turn and records how long each stage took, so
latency can be attributed to a specific stage instead of guessed at.

The id is carried in a ContextVar rather than threaded through function
signatures, so instrumenting a stage costs one line and no call-site changes.

Output (INFO, one line per stage):

    [TRACE 3f9c1a] +1243ms  model_call_done  provider=gemini model=...

Set NOVA_TRACE=0 to disable.
"""
from __future__ import annotations

import contextvars
import os
import time
import uuid
from typing import Any, Dict, List, Optional

_rid: contextvars.ContextVar[str] = contextvars.ContextVar("nova_rid", default="")
_t0: contextvars.ContextVar[float] = contextvars.ContextVar("nova_t0", default=0.0)
_marks: contextvars.ContextVar[Optional[List[Dict[str, Any]]]] = contextvars.ContextVar(
    "nova_marks", default=None
)

ENABLED = os.getenv("NOVA_TRACE", "1") != "0"


def _log():
    try:
        import nova as _nova
        return _nova.log
    except Exception:
        import logging
        return logging.getLogger("NOVA")


def start(label: str = "") -> str:
    """Begin a traced request. Returns the request id."""
    rid = uuid.uuid4().hex[:6]
    _rid.set(rid)
    _t0.set(time.time())
    _marks.set([])
    if ENABLED:
        _log().info("[TRACE %s] +0ms  start  %s", rid, label)
    return rid


def rid() -> str:
    return _rid.get()


def mark(stage: str, **fields: Any) -> float:
    """Record a stage boundary. Returns ms since the request started."""
    t0 = _t0.get()
    if not t0:
        return 0.0
    elapsed_ms = (time.time() - t0) * 1000.0
    entries = _marks.get()
    if entries is not None:
        entries.append({"stage": stage, "ms": round(elapsed_ms, 1), **fields})
    if ENABLED:
        extra = " ".join(f"{k}={v}" for k, v in fields.items())
        _log().info("[TRACE %s] +%dms  %s  %s", _rid.get(), int(elapsed_ms), stage, extra)
    return elapsed_ms


def marks() -> List[Dict[str, Any]]:
    return list(_marks.get() or [])


def summary() -> Dict[str, Any]:
    """Per-stage deltas for the finished request."""
    entries = marks()
    out = []
    prev = 0.0
    for e in entries:
        out.append({"stage": e["stage"], "at_ms": e["ms"], "delta_ms": round(e["ms"] - prev, 1)})
        prev = e["ms"]
    return {"rid": _rid.get(), "total_ms": round(prev, 1), "stages": out}


__all__ = ["start", "mark", "marks", "summary", "rid", "ENABLED"]
