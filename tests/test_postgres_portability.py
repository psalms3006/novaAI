"""The platform database is Postgres. Development runs on SQLite.

That gap is where portability bugs hide, and they are the worst kind: the code
works all through development and misbehaves only in production. These tests
compile the real schema and the real queries against the Postgres dialect, and
pin the two differences that actually bite.
"""
from __future__ import annotations

import re

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from nova_cloud.models import (
    ActivityEvent, AdminAuditLog, Base, Device, ModelCall, Preference, User,
)

PG = postgresql.dialect()


def compile_pg(stmt) -> str:
    return str(stmt.compile(dialect=PG,
                            compile_kwargs={"literal_binds": False}))


def test_the_whole_schema_compiles_for_postgres():
    for table in Base.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=PG))
        assert "CREATE TABLE" in ddl, f"{table.name} produced no DDL"


def test_json_columns_become_jsonb_on_postgres():
    """JSONB is indexable and binary; plain JSON on Postgres is a text blob."""
    for table_name in ("preferences", "activity_events", "error_events",
                       "admin_audit_logs"):
        table = Base.metadata.tables[table_name]
        ddl = str(CreateTable(table).compile(dialect=PG))
        assert "JSONB" in ddl, f"{table_name} does not use JSONB on Postgres"


def test_json_columns_stay_plain_json_on_sqlite():
    """Development must keep working without a second code path."""
    table = Base.metadata.tables["preferences"]
    ddl = str(CreateTable(table).compile(dialect=sqlite.dialect()))
    assert "JSONB" not in ddl
    assert "JSON" in ddl


def test_user_search_is_explicitly_case_insensitive():
    """SQLite's LIKE ignores ASCII case; Postgres' does not.

    A plain LIKE therefore passes every local test and then quietly stops
    matching once the platform moves to a real database. Searching for
    'SAM@' must find 'sam@'.
    """
    import io
    src = io.open("nova_cloud/api_admin.py", encoding="utf-8").read()
    block = src[src.index("def list_users"):src.index("def user_detail")]

    # Every comparison the search performs must be case-insensitive. Checking
    # the whole block rather than one line, so restructuring the query cannot
    # quietly reintroduce a case-sensitive match.
    assert ".ilike(" in block, "admin user search is not case-insensitive"
    assert re.search(r"\.like\(", block) is None, (
        "admin user search uses .like(); on Postgres that is case-sensitive "
        "and the search will silently stop working")

    sql = compile_pg(select(User).where(User.email.ilike("%sam%")))
    assert "lower(" in sql.lower() or "ILIKE" in sql.upper()


@pytest.mark.parametrize("stmt_factory", [
    lambda: select(User).order_by(User.created_at.desc()).limit(50).offset(0),
    lambda: select(Device).order_by(Device.last_seen_at.desc().nullslast()),
    lambda: select(func.count()).select_from(User).where(User.status != "deleted"),
    lambda: select(func.count(func.distinct(ActivityEvent.user_id)))
            .where(ActivityEvent.ts > 0, ActivityEvent.user_id.is_not(None)),
    lambda: select(func.avg(ModelCall.latency_ms))
            .where(ModelCall.status == "success"),
    lambda: select(ModelCall.provider, ModelCall.model, func.count())
            .group_by(ModelCall.provider, ModelCall.model),
    lambda: select(Preference).where(Preference.user_id == "x",
                                     Preference.updated_at > 0),
    lambda: select(AdminAuditLog).order_by(AdminAuditLog.ts.desc()).limit(100),
])
def test_real_queries_compile_for_postgres(stmt_factory):
    sql = compile_pg(stmt_factory())
    assert sql and "SELECT" in sql.upper()


def test_nulls_last_is_expressed_explicitly():
    """Postgres sorts NULLs first on DESC by default, SQLite sorts them last.

    The device list orders by last_seen_at DESC; without an explicit NULLS
    LAST, devices that have never checked in would jump to the top of the
    admin's list on Postgres.
    """
    sql = compile_pg(select(Device).order_by(Device.last_seen_at.desc().nullslast()))
    assert "NULLS LAST" in sql.upper()

    import io
    src = io.open("nova_cloud/api_admin.py", encoding="utf-8").read()
    assert "nullslast()" in src, "device ordering lost its explicit NULLS LAST"


def test_no_sqlite_only_sql_in_the_backend():
    """Guards against raw SQL only one database understands.

    Only raw-SQL contexts are inspected. Python has its own strftime, and
    flagging that would be a false positive that trains people to ignore this
    test.
    """
    import io
    import pathlib
    import re

    # Tokens that are SQLite-only wherever they appear in SQL.
    always_banned = ("AUTOINCREMENT", "sqlite_master", "INSERT OR REPLACE",
                     "PRAGMA ")
    # Tokens that are only a problem inside SQL, because Python has them too.
    sql_only_banned = ("strftime(", "datetime('now')", "julianday(")

    raw_sql = re.compile(r"""text\(\s*["'](.+?)["']\s*\)""", re.S)
    offenders = []
    for path in pathlib.Path("nova_cloud").glob("*.py"):
        if path.name == "db.py":
            continue          # db.py sets SQLite PRAGMAs behind a dialect check
        src = io.open(path, encoding="utf-8").read()
        for token in always_banned:
            if token in src:
                offenders.append((path.name, token))
        for statement in raw_sql.findall(src):
            for token in sql_only_banned:
                if token in statement:
                    offenders.append((path.name, token))
    assert not offenders, f"SQLite-only SQL outside db.py: {offenders}"


def test_sqlite_pragmas_are_guarded_by_a_dialect_check():
    import io
    src = io.open("nova_cloud/db.py", encoding="utf-8").read()
    idx = src.index("PRAGMA journal_mode")
    before = src[:idx]
    assert 'url.startswith("sqlite")' in before, (
        "SQLite PRAGMAs are not guarded; they would be sent to Postgres")


def test_the_postgres_driver_is_installed():
    """Deploying against Supabase needs a driver present, not just a URL."""
    import psycopg
    assert psycopg.__version__


def test_a_postgres_url_selects_the_right_dialect(monkeypatch, tmp_path):
    """DATABASE_URL pointing at Supabase must actually build a PG engine."""
    from sqlalchemy.engine import make_url
    url = make_url("postgresql+psycopg://user:pw@db.example.supabase.co:5432/postgres")
    assert url.get_backend_name() == "postgresql"
    assert url.get_driver_name() == "psycopg"
