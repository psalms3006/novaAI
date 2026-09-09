"""Database configuration: encoding, .env loading, and not leaking secrets.

These exist because the first attempt to connect NOVA to Supabase failed for
none of the interesting reasons. The password contained `$` and `&`; PowerShell
interpolated the `$` inside double quotes, and neither character was
percent-encoded for a URI. Both are silent failures that look like a wrong
password.
"""
from __future__ import annotations

import os
import re
from urllib.parse import quote

import pytest
from sqlalchemy.engine import make_url


NASTY = "&Q6mu$74YVijiee"          # the shape that actually broke
HARDER = "p@ss/wo?rd#[1]&x$y=z%q"


def repair(raw: str) -> str:
    """The same transformation nova_cloud.manage.set_db performs."""
    m = re.match(r"^(?P<scheme>[a-zA-Z0-9+.\-]+)://(?P<rest>.*)$", raw)
    scheme, rest = m.group("scheme"), m.group("rest")
    userinfo, _, hostpart = rest.rpartition("@")
    user, _, password = userinfo.partition(":")
    if scheme in ("postgres", "postgresql"):
        scheme = "postgresql+psycopg"
    return f"{scheme}://{quote(user, safe='')}:{quote(password, safe='')}@{hostpart}"


@pytest.mark.parametrize("password", [NASTY, HARDER, "simple", "with space"])
def test_a_password_survives_encoding_exactly(password):
    raw = (f"postgresql://postgres.ref:{password}"
           "@aws-0-eu-west-2.pooler.supabase.com:6543/postgres")
    url = make_url(repair(raw))
    assert url.password == password, "the password did not round-trip"


def test_reserved_characters_are_escaped_in_the_stored_form():
    raw = f"postgresql://u:{NASTY}@host.example.com:6543/postgres"
    fixed = repair(raw)
    assert "%26" in fixed and "%24" in fixed
    assert NASTY not in fixed, "the raw password is still in the URL"


def test_a_bare_postgresql_scheme_is_upgraded_to_psycopg():
    """A bare postgresql:// makes SQLAlchemy look for psycopg2, which NOVA
    does not install."""
    fixed = repair("postgresql://u:p@host:6543/postgres")
    assert make_url(fixed).get_driver_name() == "psycopg"


def test_an_at_sign_in_the_password_does_not_break_host_parsing():
    """rpartition, not partition: the last @ separates credentials from host."""
    url = make_url(repair("postgresql://u:pa@ss@db.example.com:6543/postgres"))
    assert url.host == "db.example.com"
    assert url.password == "pa@ss"


def test_the_engine_builds_for_a_supabase_pooler_url():
    from sqlalchemy import create_engine
    url = make_url(repair(
        f"postgresql://postgres.ref:{NASTY}"
        "@aws-0-eu-west-2.pooler.supabase.com:6543/postgres"))
    engine = create_engine(url, pool_pre_ping=True)
    try:
        assert engine.dialect.name == "postgresql"
        assert engine.dialect.driver == "psycopg"
    finally:
        engine.dispose()


def test_config_reads_dotenv(monkeypatch, tmp_path):
    """A shell variable lives only in the window that set it."""
    env = tmp_path / ".env"
    env.write_text("DATABASE_URL=postgresql+psycopg://u:p@h:6543/db\n",
                   encoding="utf-8")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)

    from dotenv import load_dotenv
    load_dotenv(env, override=True)
    assert os.getenv("DATABASE_URL", "").startswith("postgresql+psycopg://")


def test_nova_cloud_config_loads_dotenv_at_import():
    import io
    src = io.open("nova_cloud/config.py", encoding="utf-8").read()
    assert "load_dotenv" in src, "config.py does not read .env"
    assert src.index("load_dotenv") < src.index("class Config"), \
        ".env must be loaded before the config reads the environment"


def test_the_checker_never_prints_the_password():
    """A diagnostic that leaks the secret is worse than no diagnostic."""
    import io
    src = io.open("tools/check_db.py", encoding="utf-8").read()
    assert "url.password" in src
    # every use must be a boolean test, a scrub, or a character-class check
    for line in src.splitlines():
        if "url.password" in line and "print" in line:
            assert "bool(" in line, f"password may be printed: {line.strip()}"


def test_the_checker_scrubs_credentials_from_error_text():
    import io
    src = io.open("tools/check_db.py", encoding="utf-8").read()
    assert "_scrub(" in src
    scrub = src[src.index("def _scrub("):]
    assert "url.password" in scrub and "url.username" in scrub


def test_dotenv_is_gitignored():
    import io
    ignored = io.open(".gitignore", encoding="utf-8").read()
    assert ".env" in ignored


def test_no_env_file_is_tracked_by_git():
    import subprocess
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True)
    for path in out.stdout.split():
        base = path.rsplit("/", 1)[-1]
        if base == ".env":
            pytest.fail(f"a real .env is tracked: {path}")


# -- transaction pooler compatibility ----------------------------------------

def _engine_kwargs_for(url: str) -> dict:
    """Reproduce the branch nova_cloud.db.engine() takes for a given URL."""
    from sqlalchemy.engine import make_url
    kw = {"future": True, "pool_pre_ping": True}
    if url.startswith("postgresql"):
        parsed = make_url(url)
        pooled = (parsed.port == 6543
                  or "pooler" in (parsed.host or "").lower()
                  or "pgbouncer" in (parsed.host or "").lower())
        if pooled:
            kw["connect_args"] = {"prepare_threshold": None}
            kw["pool_size"] = 5
            kw["max_overflow"] = 5
            kw["pool_recycle"] = 300
    return kw


@pytest.mark.parametrize("url", [
    "postgresql+psycopg://u:p@aws-0-eu-west-2.pooler.supabase.com:6543/postgres",
    "postgresql+psycopg://u:p@somewhere.example.com:6543/postgres",
    "postgresql+psycopg://u:p@my-pgbouncer.internal:5432/postgres",
])
def test_prepared_statements_are_disabled_behind_a_transaction_pooler(url):
    """The bug this prevents:

    Supabase's pooler runs in transaction mode, handing one server connection
    to different clients between statements. psycopg 3 prepares statements
    automatically, so the second client to reuse a backend hits
    `DuplicatePreparedStatement: "_pg3_0" already exists` and the request
    fails. It appears only after a connection has been recycled, so a first
    run looks perfectly healthy -- which is exactly how it got missed.
    """
    kw = _engine_kwargs_for(url)
    assert kw.get("connect_args", {}).get("prepare_threshold", "unset") is None, \
        "prepared statements are still enabled behind a transaction pooler"


def test_a_direct_connection_keeps_prepared_statements():
    """They are a real performance win when the connection is not shared."""
    kw = _engine_kwargs_for(
        "postgresql+psycopg://u:p@db.abcdefgh.supabase.co:5432/postgres")
    assert "prepare_threshold" not in kw.get("connect_args", {})


def test_sqlite_is_unaffected():
    kw = _engine_kwargs_for("sqlite:///local.db")
    assert "prepare_threshold" not in kw.get("connect_args", {})


def test_the_pooler_branch_exists_in_the_real_engine_builder():
    """Guards against the fix being refactored away."""
    import io
    src = io.open("nova_cloud/db.py", encoding="utf-8").read()
    assert "prepare_threshold" in src
    assert "6543" in src or "pooler" in src
