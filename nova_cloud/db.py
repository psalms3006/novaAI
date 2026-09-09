"""nova_cloud.db — engine, sessions, schema creation and rate limiting."""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from .config import config
from .models import Base, RateLimitBucket

log = logging.getLogger("nova.db")

_engine = None
_SessionLocal = None
_lock = threading.Lock()


def engine():
    global _engine, _SessionLocal
    with _lock:
        if _engine is None:
            url = config().database_url
            kw: dict = {"future": True, "pool_pre_ping": True}

            if url.startswith("postgresql"):
                from sqlalchemy.engine import make_url
                parsed = make_url(url)
                pooled = (parsed.port == 6543
                          or "pooler" in (parsed.host or "").lower()
                          or "pgbouncer" in (parsed.host or "").lower())
                if pooled:
                    # Supabase's pooler (and PgBouncer generally) runs in
                    # transaction mode, where a server connection is handed to
                    # a different client between statements. psycopg 3 prepares
                    # statements automatically, so the second connection to
                    # reuse a backend hits
                    #     DuplicatePreparedStatement: "_pg3_0" already exists
                    # and the request fails. It surfaces only after a
                    # connection has been recycled, which is why a first run
                    # can look perfectly healthy.
                    kw["connect_args"] = {"prepare_threshold": None}
                    # Transaction pooling also makes a large client-side pool
                    # pointless: the pooler is the pool.
                    kw["pool_size"] = 5
                    kw["max_overflow"] = 5
                    kw["pool_recycle"] = 300
                    log.info("[DB] transaction pooler detected; prepared "
                             "statements disabled")

            if url.startswith("sqlite"):
                # SQLite needs WAL to tolerate concurrent readers alongside the
                # writer, and check_same_thread off because Flask serves
                # requests on worker threads.
                kw["connect_args"] = {"check_same_thread": False, "timeout": 30}
            _engine = create_engine(url, **kw)
            if url.startswith("sqlite"):
                @event.listens_for(_engine, "connect")
                def _pragmas(dbapi_conn, _rec):   # noqa: ANN001
                    cur = dbapi_conn.cursor()
                    cur.execute("PRAGMA journal_mode=WAL")
                    cur.execute("PRAGMA synchronous=NORMAL")
                    cur.execute("PRAGMA foreign_keys=ON")
                    cur.close()
            _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False,
                                         future=True)
        return _engine


def init_db() -> None:
    Base.metadata.create_all(engine())


def reset_engine() -> None:
    """Test hook: drop the cached engine so a new DATABASE_URL takes effect."""
    global _engine, _SessionLocal
    with _lock:
        if _engine is not None:
            try:
                _engine.dispose()
            except Exception:
                pass
        _engine = None
        _SessionLocal = None


@contextmanager
def session_scope() -> Iterator[Session]:
    engine()
    s = _SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


# -- rate limiting -----------------------------------------------------------

class RateLimited(Exception):
    def __init__(self, retry_after: int):
        super().__init__("rate limited")
        self.retry_after = retry_after


def rate_limit(session: Session, key: str, *, limit: int, window_s: int) -> None:
    """Fixed-window counter, persisted so restarts do not reset the limit.

    Raises RateLimited when the caller is over budget. Kept in the database
    rather than in memory because the login and signup endpoints are exactly
    what an attacker retries, and an in-process counter would be defeated by a
    restart or a second worker.
    """
    now = time.time()
    row = session.get(RateLimitBucket, key)
    if row is None:
        session.add(RateLimitBucket(key=key, window_start=now, count=1))
        return
    if now - row.window_start >= window_s:
        row.window_start = now
        row.count = 1
        return
    row.count += 1
    if row.count > limit:
        raise RateLimited(retry_after=int(window_s - (now - row.window_start)) + 1)


def purge_rate_limits(session: Session, older_than_s: int = 3600) -> int:
    cutoff = time.time() - older_than_s
    rows = session.scalars(
        select(RateLimitBucket).where(RateLimitBucket.window_start < cutoff)
    ).all()
    for r in rows:
        session.delete(r)
    return len(rows)


__all__ = ["engine", "init_db", "reset_engine", "session_scope", "rate_limit",
           "RateLimited", "purge_rate_limits"]
