"""nova_core.cancel — a tool call that was withdrawn must not run.

Voice runs each tool on a worker thread. When the model withdraws a call
(usually because the person spoke over it), the live session sets that
call's event. The dispatcher checks it immediately before acting, and a
confirmation waiting for an answer closes itself. On 2026-09-30 a shutdown
the model had withdrawn four seconds earlier ran anyway.
"""
from __future__ import annotations

import threading
from typing import Optional

_local = threading.local()


def set_current(event: Optional[threading.Event]) -> None:
    _local.event = event


def current() -> Optional[threading.Event]:
    return getattr(_local, "event", None)


def cancelled() -> bool:
    ev = current()
    return bool(ev is not None and ev.is_set())


WITHDRAWN = ("Not done: this request was withdrawn before it ran, so nothing happened. "
             "Ask the user again if it is still wanted.")

__all__ = ["set_current", "current", "cancelled", "WITHDRAWN"]
