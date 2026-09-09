"""Read-only connectivity check against the configured database.

Run it in the shell where DATABASE_URL is set:

    python tools/check_db.py

It never prints the URL, the host credentials or the password. It only reads:
no table is created, altered or dropped, so it is safe to run against a
production database.

Exit codes:  0 reachable   1 configuration problem   2 connection failed
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: Characters that must be percent-encoded inside the password of a URI.
#: Supabase generates passwords containing them regularly, and an unencoded
#: one is the single most common reason a correct password appears wrong.
_MUST_ENCODE = "@/?#[]&$+,;=% "
_ENCODINGS = {"@": "%40", "/": "%2F", "?": "%3F", "#": "%23", "[": "%5B",
              "]": "%5D", "&": "%26", "$": "%24", "+": "%2B", ",": "%2C",
              ";": "%3B", "=": "%3D", "%": "%25", " ": "%20"}


def main() -> int:
    raw = os.getenv("DATABASE_URL", "").strip()
    if not raw:
        print("DATABASE_URL is not set in this shell.", file=sys.stderr)
        print("Set it, then run this again from the same shell.", file=sys.stderr)
        return 1

    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import SQLAlchemyError

    # -- shape ------------------------------------------------------------
    try:
        url = make_url(raw)
    except Exception as e:
        print(f"DATABASE_URL could not be parsed as a URL ({type(e).__name__}).",
              file=sys.stderr)
        return 1

    print("configuration")
    print(f"  backend            {url.get_backend_name()}")
    print(f"  driver             {url.get_driver_name() or '(default)'}")
    print(f"  host               {_mask_host(url.host)}")
    print(f"  port               {url.port}")
    print(f"  database           {url.database}")
    print(f"  username set       {bool(url.username)}")
    print(f"  password set       {bool(url.password)}")

    problems = []
    if url.get_backend_name() != "postgresql":
        problems.append("the URL is not a PostgreSQL URL")
    if url.get_driver_name() != "psycopg":
        problems.append("use the postgresql+psycopg:// scheme; a bare "
                        "postgresql:// makes SQLAlchemy look for psycopg2, "
                        "which is not installed")
    if url.port == 5432:
        print("  note               port 5432 is the direct connection; the "
              "pooler (6543) is the right choice for a web app")

    # Detect an unencoded special character without revealing the password.
    pw = url.password or ""
    offenders = sorted({c for c in pw if c in _MUST_ENCODE})
    if offenders:
        hint = ", ".join(f"{c!r} -> {_ENCODINGS[c]}" for c in offenders)
        problems.append(
            "the password contains characters that must be percent-encoded "
            f"inside a URL ({hint}). SQLAlchemy has parsed it, but some tools "
            "will read it differently")

    if problems:
        print("\nproblems")
        for p in problems:
            print(f"  - {p}")
        if any("psycopg" in p or "not a PostgreSQL" in p for p in problems):
            return 1

    # -- connect (read only) ----------------------------------------------
    print("\nconnecting (read-only)")
    engine = create_engine(url, pool_pre_ping=True,
                           connect_args={"connect_timeout": 15})
    started = time.time()
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            elapsed = (time.time() - started) * 1000
            print(f"  reachable          yes ({elapsed:.0f} ms)")

            version = conn.execute(text("SHOW server_version")).scalar()
            print(f"  server version     PostgreSQL {version}")
            print(f"  current database   "
                  f"{conn.execute(text('SELECT current_database()')).scalar()}")
            print(f"  connected as       "
                  f"{conn.execute(text('SELECT current_user')).scalar()}")

            rows = conn.execute(text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' ORDER BY tablename")).fetchall()
            existing = {r[0] for r in rows}

            from nova_cloud.models import Base
            expected = set(Base.metadata.tables)
            print(f"\nschema")
            print(f"  tables in public   {len(existing)}")
            print(f"  NOVA expects       {len(expected)}")
            present = expected & existing
            missing = expected - existing
            foreign = existing - expected
            print(f"  already present    {len(present)}")
            print(f"  missing            {len(missing)}")
            if foreign:
                print(f"  not NOVA's         {len(foreign)} "
                      f"({', '.join(sorted(foreign)[:5])}"
                      f"{'...' if len(foreign) > 5 else ''})")
            if missing:
                print(f"\n  missing tables: {', '.join(sorted(missing))}")
    except SQLAlchemyError as e:
        # The driver puts the host in some messages, never the password, but
        # trim it anyway rather than risk it.
        reason = type(e).__name__
        detail = str(getattr(e, "orig", e)).splitlines()[0][:160]
        print(f"  reachable          NO", file=sys.stderr)
        print(f"\nconnection failed: {reason}", file=sys.stderr)
        print(f"  {_scrub(detail, url)}", file=sys.stderr)
        print("\nNothing was modified.", file=sys.stderr)
        return 2
    finally:
        engine.dispose()

    print("\nNothing was modified. The database is reachable.")
    if missing:
        print("\nNext step, to create the missing tables:")
        print('  python -c "from nova_cloud.db import init_db; init_db()"')
    else:
        print("\nThe NOVA schema is already present; nothing to create.")
    return 0


def _mask_host(host: str | None) -> str:
    if not host:
        return "(none)"
    parts = host.split(".")
    if len(parts) > 2:
        return f"{parts[0][:6]}***." + ".".join(parts[-3:])
    return host


def _scrub(text_: str, url) -> str:
    out = text_
    for secret in (url.password, url.username):
        if secret:
            out = out.replace(str(secret), "***")
    return out


if __name__ == "__main__":
    raise SystemExit(main())
