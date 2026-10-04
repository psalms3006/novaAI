"""What NOVA has been doing while nobody was watching.

Once work happens outside a conversation -- a mail sweep at nine, a workflow
that runs twice a day for a month -- "what have you been doing?" can only be
answered honestly from a record of what happened. The model was not present
for any of it, and asking it to recall is asking it to invent.

This is a thin layer over `orchestrator/audit_logger.py`, which already had
the right shape: append-only NDJSON, bounded, with secret redaction built in.
It was only ever reachable from the orchestrator path, which nothing outside
the tests imports, so it is wired up here rather than rewritten.

Two rules about what goes in.

Credentials never do. The underlying logger redacts on the way in, so a token
passed by mistake is dropped rather than persisted -- and that is worth having
even though nothing here passes one deliberately, because the mistake is the
case it exists for.

Neither does the user's content. An audit trail records that three messages
looked worth attention and why one of them scored, not what the messages said.
It is a record of NOVA's behaviour, not a second copy of the inbox.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

log = logging.getLogger("nova.activity")

__all__ = ["ActivityTrail", "get_activity_trail"]


class ActivityTrail:
    """Records autonomous activity, and recounts it in plain language."""

    def __init__(self, path: Optional[str] = None) -> None:
        from orchestrator.audit_logger import AuditLogger
        self._audit = AuditLogger(path=path)

    # ── writing ─────────────────────────────────────────────────────────────
    def record(self, event: str, **data: Any) -> None:
        """Never raises. Auditing must not take down what it is auditing."""
        try:
            self._audit.record(event, **data)
        except Exception:
            log.debug("[ACTIVITY] could not record %s", event, exc_info=True)

    def workflow_started(self, workflow_id: str, title: str) -> None:
        self.record("workflow.started", workflow_id=workflow_id, title=title)

    def workflow_finished(self, workflow_id: str, title: str,
                          outcome: str = "done", detail: str = "") -> None:
        self.record("workflow.finished", workflow_id=workflow_id, title=title,
                    outcome=outcome, detail=detail)

    def workflow_skipped(self, workflow_id: str, title: str,
                         reason: str = "") -> None:
        self.record("workflow.skipped", workflow_id=workflow_id, title=title,
                    reason=reason)

    def mail_checked(self, found: int, top_reason: str = "") -> None:
        """How many messages looked notable, and why one of them did.

        Deliberately not what any of them said.
        """
        self.record("mail.checked", found=int(found), top_reason=top_reason)

    def spoke(self, kind: str, summary: str = "") -> None:
        self.record("user.notified", kind=kind, summary=summary[:160])

    # ── reading ─────────────────────────────────────────────────────────────
    def recent(self, limit: int = 20) -> list[dict]:
        try:
            return list(reversed(self._audit.recent(limit=limit)))
        except Exception:
            log.debug("[ACTIVITY] could not read the trail", exc_info=True)
            return []

    def narrate(self, limit: int = 8) -> str:
        """The answer to "what have you been doing?", from the record.

        Most recent first, because that is the part someone asking has in
        mind, and short enough to be said out loud.
        """
        entries = self.recent(limit=limit)
        if not entries:
            return "Nothing yet — I haven't done anything on my own."

        lines = []
        for entry in entries[:limit]:
            line = self._line(entry)
            if line:
                lines.append("  " + line)

        if not lines:
            return "Nothing yet — I haven't done anything on my own."
        return "Here's what I've been doing:\n" + "\n".join(lines)

    @staticmethod
    def _line(entry: dict) -> str:
        event = entry.get("event", "")
        title = entry.get("title", "") or entry.get("workflow_id", "")
        when = ActivityTrail._when(entry.get("ts_unix"))

        if event == "workflow.finished":
            outcome = entry.get("outcome", "done")
            detail = entry.get("detail", "")
            if outcome == "done":
                return f"{when}: finished {title}."
            tail = f" ({detail})" if detail else ""
            return f"{when}: {title} {outcome}{tail}."
        if event == "workflow.skipped":
            reason = entry.get("reason", "")
            tail = f" — {reason}" if reason else ""
            return f"{when}: skipped {title}{tail}."
        if event == "workflow.started":
            return f"{when}: started {title}."
        if event == "mail.checked":
            found = entry.get("found", 0)
            if not found:
                return f"{when}: checked your mail, nothing needed you."
            reason = entry.get("top_reason", "")
            tail = f" — top one {reason}" if reason else ""
            return f"{when}: checked your mail, {found} worth a look{tail}."
        if event == "user.notified":
            return f"{when}: told you about {entry.get('kind', 'something')}."
        return ""

    @staticmethod
    def _when(ts: Any) -> str:
        try:
            delta = time.time() - float(ts)
        except Exception:
            return "recently"
        if delta < 90:
            return "just now"
        if delta < 3600:
            return f"{int(delta // 60)} min ago"
        if delta < 86400:
            return f"{int(delta // 3600)}h ago"
        return f"{int(delta // 86400)}d ago"


_trail: Optional[ActivityTrail] = None


def get_activity_trail() -> ActivityTrail:
    global _trail
    if _trail is None:
        _trail = ActivityTrail()
    return _trail
