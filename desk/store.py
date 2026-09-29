"""desk.store — persistent conversation storage for the NOVA desktop app.

SQLite database at %APPDATA%/NOVA/nova_desktop.db (WAL mode). Thread-safe via
a single writer lock. Stores conversations (id, title, timestamps) and messages
as JSON. This is UI-persistence only — the intelligence lives in nova.py.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
import time

from . settings import app_data_dir

# Reentrant: callers hold it around _conn(), which may create the schema
# (taking it again) the first time an account's database is opened.
_LOCK = threading.RLock()
def _db_path():
    # Resolved per call, not at import: the bridge is imported before anyone
    # has signed in, and the conversations belong to whoever then does.
    return app_data_dir() / "nova_desktop.db"


_READY: set = set()      # database files whose schema is known to exist


def _raw(path: str):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _conn():
    # The schema is ensured per database file, not once at import: each
    # account has its own file, and it may not exist until that person signs in.
    path = str(_db_path())
    if path not in _READY:
        _init(path)
    return _raw(path)


def _init(path: str | None = None):
    path = path or str(_db_path())
    with _LOCK:
        with _raw(path) as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS conversations (
                       id TEXT PRIMARY KEY,
                       title TEXT NOT NULL DEFAULT 'New chat',
                       project_id TEXT NOT NULL DEFAULT '',
                       created REAL NOT NULL,
                       updated REAL NOT NULL
                   )"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS messages (
                       id TEXT PRIMARY KEY,
                       conversation_id TEXT NOT NULL,
                       role TEXT NOT NULL,
                       content TEXT NOT NULL DEFAULT '',
                       meta TEXT NOT NULL DEFAULT '{}',
                       ts REAL NOT NULL
                   )"""
            )
            # migrate older DBs that predate project_id
            cols = [r[1] for r in c.execute("PRAGMA table_info(conversations)")]
            if "project_id" not in cols:
                c.execute("ALTER TABLE conversations ADD COLUMN project_id TEXT NOT NULL DEFAULT ''")
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_convo "
                "ON messages(conversation_id, ts)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversations_updated "
                "ON conversations(updated DESC)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversations_project "
                "ON conversations(project_id)"
            )
        _READY.add(path)


def new_conversation(title: str = "New chat", project_id: str = "") -> str:
    cid = uuid.uuid4().hex
    now = time.time()
    with _LOCK:
        with _conn() as c:
            c.execute(
                "INSERT INTO conversations(id, title, project_id, created, updated) VALUES (?,?,?,?,?)",
                (cid, title, project_id, now, now),
            )
    return cid


def list_conversations(limit: int = 200, project_id: str | None = None) -> list:
    with _LOCK:
        with _conn() as c:
            if project_id:
                rows = c.execute(
                    """SELECT id, title, project_id, created, updated,
                              (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=conversations.id) AS n
                       FROM conversations WHERE project_id=? ORDER BY updated DESC LIMIT ?""",
                    (project_id, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    """SELECT id, title, project_id, created, updated,
                              (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=conversations.id) AS n
                       FROM conversations ORDER BY updated DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
    return [dict(r) for r in rows]


def search_conversations(query: str, limit: int = 100) -> list:
    like = f"%{query}%"
    with _LOCK:
        with _conn() as c:
            rows = c.execute(
                """SELECT DISTINCT c.id, c.title, c.project_id, c.created, c.updated,
                          (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) AS n
                   FROM conversations c
                   LEFT JOIN messages m ON m.conversation_id = c.id
                   WHERE c.title LIKE ? OR m.content LIKE ?
                   ORDER BY c.updated DESC LIMIT ?""",
                (like, like, limit),
            ).fetchall()
    return [dict(r) for r in rows]


def get_conversation(cid: str):
    with _LOCK:
        with _conn() as c:
            c_row = c.execute(
                "SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
            if not c_row:
                return None
            m_rows = c.execute(
                "SELECT id, role, content, meta, ts FROM messages "
                "WHERE conversation_id=? ORDER BY ts ASC", (cid,)).fetchall()
    return {
        "id": c_row["id"],
        "title": c_row["title"],
        "project_id": c_row["project_id"],
        "created": c_row["created"],
        "updated": c_row["updated"],
        "messages": [dict(m) for m in m_rows],
    }


def _touch(cid: str):
    with _conn() as c:
        c.execute("UPDATE conversations SET updated=? WHERE id=?", (time.time(), cid))


def add_message(cid: str, role: str, content: str, meta: dict | None = None) -> str:
    mid = uuid.uuid4().hex
    now = time.time()
    with _LOCK:
        with _conn() as c:
            c.execute(
                "INSERT INTO messages(id, conversation_id, role, content, meta, ts) "
                "VALUES (?,?,?,?,?,?)",
                (mid, cid, role, content, json.dumps(meta or {}), now),
            )
            c.execute(
                "UPDATE conversations SET updated=? WHERE id=?",
                (now, cid),
            )
    return mid


def update_message(mid: str, content: str | None = None, meta: dict | None = None):
    sets, vals = [], []
    if content is not None:
        sets.append("content=?")
        vals.append(content)
    if meta is not None:
        sets.append("meta=?")
        vals.append(json.dumps(meta))
    if not sets:
        return
    vals.append(mid)
    with _LOCK:
        with _conn() as c:
            c.execute(f"UPDATE messages SET {', '.join(sets)} WHERE id=?", vals)


def last_message(cid: str, role: str | None = None):
    with _LOCK:
        with _conn() as c:
            if role:
                row = c.execute(
                    "SELECT * FROM messages WHERE conversation_id=? AND role=? "
                    "ORDER BY ts DESC LIMIT 1", (cid, role)).fetchone()
            else:
                row = c.execute(
                    "SELECT * FROM messages WHERE conversation_id=? "
                    "ORDER BY ts DESC LIMIT 1", (cid,)).fetchone()
    return dict(row) if row else None


def upsert_last_assistant(cid: str, content: str, meta: dict | None = None) -> str:
    """Update the newest assistant message or insert one."""
    last = last_message(cid, role="assistant")
    if last is not None and time.time() - last["ts"] < 1.0:
        update_message(last["id"], content=content, meta=meta)
        return last["id"]
    return add_message(cid, "assistant", content, meta or {})


def rename_conversation(cid: str, title: str) -> bool:
    with _LOCK:
        with _conn() as c:
            c.execute("UPDATE conversations SET title=? WHERE id=?", (title, cid))
    return True


def set_conversation_project(cid: str, project_id: str) -> bool:
    with _LOCK:
        with _conn() as c:
            c.execute("UPDATE conversations SET project_id=? WHERE id=?", (project_id, cid))
    return True


def count_by_project(project_id: str) -> int:
    with _LOCK:
        with _conn() as c:
            return c.execute(
                "SELECT COUNT(*) AS n FROM conversations WHERE project_id=?",
                (project_id,)).fetchone()["n"]


def delete_message(mid: str) -> bool:
    with _LOCK:
        with _conn() as c:
            c.execute("DELETE FROM messages WHERE id=?", (mid,))
    return True


def delete_conversation(cid: str) -> bool:
    with _LOCK:
        with _conn() as c:
            c.execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
            c.execute("DELETE FROM conversations WHERE id=?", (cid,))
    return True


def clear_all() -> int:
    with _LOCK:
        with _conn() as c:
            c.execute("DELETE FROM messages")
            n = c.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"]
            c.execute("DELETE FROM conversations")
    return n