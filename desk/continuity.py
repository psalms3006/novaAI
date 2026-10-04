"""desk.continuity — where you left off.

The session archive was described as "what NOVA reads to know what you talked
about last time", and nothing read it: a new conversation started with no idea
of the last one or of work still under way. (The Zoey audit's "working memory":
pick up where I left off.) This builds a few lines from what is already
stored -- the most recent earlier conversations, typed or spoken, and
background tasks that have not finished -- for the start of a session.

Short on purpose: it is a pointer, not a transcript. recall_conversations is
there for the detail.
"""
from __future__ import annotations

import time

_DONE = {"COMPLETED", "FAILED", "CANCELLED", "CANCELED", "PARTIAL", "PARTIALLY_COMPLETED"}


def _when(ts: float) -> str:
    days = (time.time() - ts) / 86400
    if days < 1 and time.localtime(ts).tm_yday == time.localtime().tm_yday:
        return "earlier today " + time.strftime("%H:%M", time.localtime(ts))
    if days < 2:
        return "yesterday " + time.strftime("%H:%M", time.localtime(ts))
    return time.strftime("%a %d %b", time.localtime(ts))


def _recent_conversations(exclude_cid: str, n: int = 2) -> list:
    from desk import store
    out = []
    for c in store.list_conversations(limit=6):
        if c["id"] == exclude_cid or not c.get("n"):
            continue
        convo = store.get_conversation(c["id"]) or {}
        users = [m["content"] for m in convo.get("messages", []) if m["role"] == "user" and m["content"].strip()]
        if not users:
            continue
        kind = "voice" if (c.get("title") or "").startswith("Voice") else "typed"
        last = " ".join(users[-1].split())[:140]
        out.append(f"- {_when(c['updated'])} ({kind}, {len(users)} message(s)); you last said: \"{last}\"")
        if len(out) >= n:
            break
    return out


def _unfinished_tasks(n: int = 3) -> list:
    try:
        from task_manager import get_task_manager
        tm = get_task_manager()
        tasks = list(getattr(tm, "_tasks", []) or []) if tm else []
    except Exception:
        return []
    live = [t for t in tasks if str(getattr(t, "status", "")).upper() not in _DONE]
    live.sort(key=lambda t: -float(getattr(t, "updated", 0) or 0))
    return [f"- unfinished task: {getattr(t, 'title', '')[:100]} ({str(getattr(t, 'status', '')).lower()})"
            for t in live[:n]]


def last_time(exclude_cid: str = "") -> str:
    try:
        lines = _recent_conversations(exclude_cid) + _unfinished_tasks()
    except Exception:
        return ""
    if not lines:
        return ""
    return ("## Where you left off\n"
            "Context from earlier sessions, not a request. Mention it only if it helps; "
            "use recall_conversations for detail.\n" + "\n".join(lines))


__all__ = ["last_time"]
