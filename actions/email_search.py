"""actions.email_search — find mail when asked.

NOVA already sweeps Gmail in the background for mail worth mentioning
(integrations/gmail.py, read-only scope). What she could not do was answer
"find the emails from Ada about the invoice" or "what's unread from this week":
the connector had no search, and no tool reached it.

Read-only by construction -- the token is `gmail.readonly`; sending or deleting
is not possible from here. Results are sender, subject, date and Gmail's own
snippet, never message bodies, and the snippets are attacker-controlled text:
the chat trust layer treats this tool's output as untrusted.
"""
from __future__ import annotations

import time


def _connector():
    from integrations.gmail import GmailConnector
    return GmailConnector()


def execute(args: dict) -> str:
    query = str(args.get("query") or "").strip()
    mode = str(args.get("mode") or ("search" if query else "important")).strip().lower()
    try:
        limit = max(1, min(25, int(args.get("limit") or 10)))
    except (TypeError, ValueError):
        limit = 10
    try:
        gm = _connector()
    except Exception as e:
        return f"Mail isn't available on this computer ({type(e).__name__})."
    h = gm.health()
    if not h.get("can_read"):
        return ("Gmail isn't connected, so I can't read your mail. You can connect it in "
                "Settings -> Accounts (read-only access); after that I can search it.")
    from integrations.gmail import GmailUnavailable
    try:
        if mode == "important":
            days = int(args.get("days") or 1)
            return gm.summarise_since(time.time() - days * 86400)
        msgs = gm.search(query, limit=limit)
    except GmailUnavailable as e:
        return f"I couldn't search your mail: {e}"
    if not msgs:
        return f"No mail matches {query!r}."
    lines = [f"{len(msgs)} message(s) matching {query!r} (newest first; sender, subject, Gmail's preview):"]
    for m in msgs:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(m.received_at)) if m.received_at else "?"
        lines.append(f"- [{when}] {m.sender} -- {m.subject or '(no subject)'}\n  {m.snippet[:200]}")
    return "\n".join(lines)


__all__ = ["execute"]
