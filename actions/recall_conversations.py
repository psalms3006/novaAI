"""actions.recall_conversations — what did we talk about?

"What did we say about the VPN yesterday?" had no answer: typed and spoken
conversations are stored (desk/store.py; voice turns since the live session
archives them, and older voice sessions in nova_memories/session_archive.jsonl),
but nothing let NOVA search them. Memory keeps facts; this finds the
conversation itself, by topic and by date, and quotes it.

Read-only. Returns dated excerpts -- what the user said and what NOVA
answered -- best matches first.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from typing import Optional

_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "with", "is", "are", "was",
         "what", "how", "why", "did", "we", "us", "you", "i", "me", "my", "about", "talk", "talked",
         "said", "say", "discuss", "discussed", "conversation", "that", "this", "it", "do", "can"}


def _terms(q: str) -> list:
    return [w for w in re.findall(r"[a-z0-9]+", (q or "").lower()) if w not in _STOP and len(w) > 1]


def parse_when(when: str, now: Optional[datetime] = None) -> tuple:
    """(start, end) epoch seconds for 'today', 'yesterday', 'last week', 'this
    week', 'N days ago', 'last N days' or 'YYYY-MM-DD'; (0, inf) when empty."""
    now = now or datetime.now()
    w = (when or "").strip().lower()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if not w:
        return 0.0, float("inf")
    if w == "today":
        return day0.timestamp(), float("inf")
    if w == "yesterday":
        return (day0 - timedelta(days=1)).timestamp(), day0.timestamp()
    m = re.match(r"(\d+)\s*days?\s*ago", w)
    if m:
        d = day0 - timedelta(days=int(m.group(1)))
        return d.timestamp(), (d + timedelta(days=1)).timestamp()
    m = re.match(r"(?:last|past)\s*(\d+)\s*days?", w)
    if m:
        return (day0 - timedelta(days=int(m.group(1)))).timestamp(), float("inf")
    if w == "this week":
        return (day0 - timedelta(days=day0.weekday())).timestamp(), float("inf")
    if w in ("last week", "past week"):
        return (day0 - timedelta(days=7)).timestamp(), float("inf")
    if w in ("last month", "past month"):
        return (day0 - timedelta(days=31)).timestamp(), float("inf")
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", w)
    if m:
        d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return d.timestamp(), (d + timedelta(days=1)).timestamp()
    return 0.0, float("inf")


def _score(text: str, terms: list) -> int:
    low = (text or "").lower()
    return sum(1 for t in terms if t in low)


def _from_store(terms: list, start: float, end: float) -> list:
    """Matching exchanges from typed and voice conversations in the local database."""
    from desk import store
    with store._LOCK:
        with store._conn() as c:
            rows = c.execute(
                "SELECT m.conversation_id AS cid, m.role, m.content, m.ts, c.title "
                "FROM messages m JOIN conversations c ON c.id = m.conversation_id "
                "WHERE m.ts >= ? AND m.ts < ? ORDER BY m.conversation_id, m.ts ASC",
                (start, end if end != float("inf") else 1e12)).fetchall()
    rows = [dict(r) for r in rows]
    out = []
    for i, r in enumerate(rows):
        s = _score(r["content"], terms)
        if not s:
            continue
        # the exchange: this message and its neighbour in the same conversation
        if r["role"] == "user":
            nxt = rows[i + 1] if i + 1 < len(rows) and rows[i + 1]["cid"] == r["cid"] else None
            user, nova = r["content"], (nxt or {}).get("content", "")
        else:
            prv = rows[i - 1] if i > 0 and rows[i - 1]["cid"] == r["cid"] else None
            user, nova = (prv or {}).get("content", ""), r["content"]
        kind = "voice" if (r.get("title") or "").startswith("Voice") else "typed"
        out.append({"ts": r["ts"], "kind": kind, "title": r.get("title") or "", "user": user,
                    "nova": nova, "score": s + _score(user + " " + nova, terms) * 0.5})
    return out


def _from_archive(terms: list, start: float, end: float) -> list:
    """Older voice sessions, kept only in the session archive."""
    try:
        from nova_memory import _default_memory_dir
        path = _default_memory_dir() / "session_archive.jsonl"
    except Exception:
        return []
    if not path.exists():
        return []
    out, seen = [], set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            sess = json.loads(line)
        except Exception:
            continue
        turns = sess.get("turns") or []
        for i, t in enumerate(turns):
            ts = float(t.get("timestamp") or 0)
            key = (ts, t.get("role"))
            if key in seen or not (start <= ts < end):
                continue
            seen.add(key)
            s = _score(t.get("content", ""), terms)
            if not s:
                continue
            if t.get("role") == "user":
                nxt = turns[i + 1] if i + 1 < len(turns) else {}
                user = t.get("content", "")
                nova = nxt.get("content", "") if nxt.get("role") != "user" else ""
            else:
                prv = turns[i - 1] if i > 0 else {}
                user = prv.get("content", "") if prv.get("role") == "user" else ""
                nova = t.get("content", "")
            out.append({"ts": ts, "kind": "voice", "title": "Voice session", "user": user, "nova": nova,
                        "score": s + _score(user + " " + nova, terms) * 0.5})
    return out


def recall(query: str, when: str = "", limit: int = 6) -> list:
    terms = _terms(query)
    if not terms:
        return []
    start, end = parse_when(when)
    found = _from_store(terms, start, end) + _from_archive(terms, start, end)
    # the same exchange can be in both places
    uniq, keys = [], set()
    for f in sorted(found, key=lambda f: (-f["score"], -f["ts"])):
        k = (f["user"][:80], f["nova"][:80])
        if k in keys:
            continue
        keys.add(k)
        uniq.append(f)
    return uniq[:limit]


def execute(args: dict) -> str:
    query = str(args.get("query") or args.get("topic") or "").strip()
    when = str(args.get("when") or "").strip()
    try:
        limit = max(1, min(15, int(args.get("limit") or 6)))
    except (TypeError, ValueError):
        limit = 6
    if not query:
        return "Say what the conversation was about (a topic, a name, a word that was used)."
    hits = recall(query, when, limit)
    span = f" ({when})" if when else ""
    if not hits:
        return f"I found no conversation about {query!r}{span} in your typed or voice history."
    lines = [f"Conversations about {query!r}{span}, best matches first:"]
    for h in hits:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(h["ts"]))
        lines.append(f"- [{stamp}, {h['kind']}] You: {h['user'][:300]}"
                     + (f"\n  NOVA: {h['nova'][:400]}" if h["nova"] else ""))
    return "\n".join(lines)


__all__ = ["execute", "recall", "parse_when"]
