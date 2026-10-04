"""NOVA must know who she is talking to, on every surface.

Two separate failures produced the same complaint — "she doesn't know who I
am", after weeks of being told.

The first was an import cycle. `memory_extra` imports `nova`, and `nova`
imports `memory_extra`, so importing memory first raises on a
partially-initialised module. That was swallowed into an empty dict, which is
not "memory unavailable" but NOVA with amnesia — and it was silent. The
terminal never hit it, because nova.py is its entry point and is therefore
always imported first; the desktop is the other way round. A large part of
"the app and the terminal aren't behaving alike" was exactly this.

The second is that identity was left to semantic recall. Memory had
accumulated five different answers to "what is the user called" — a full name,
two nicknames and two corrections — and retrieval returned whichever was
closest to the question. A name that competes with four others loses about as
often as it wins.
"""
from __future__ import annotations

import inspect

from desk import live_session as ls


# ── the import cycle ─────────────────────────────────────────────────────────

def test_memory_loads_in_the_desktops_import_order():
    """This module imported before nova is exactly the desktop's order."""
    meta = ls._load_meta()
    assert meta, "memory came back empty; NOVA starts the session knowing nothing"


def test_what_memory_holds_reaches_the_model():
    """Seeded here rather than read from whatever the machine happens to hold.

    This used to assert only that the context was non-empty, which passed
    because the developer's own memory file had things in it. Once the suite
    was given its own data directory -- it had been writing test fixtures into
    that file -- there was nothing to read and the test failed, having never
    really tested the plumbing it was named for. So it puts a fact in and
    checks that the fact comes out.
    """
    meta = dict(ls._load_meta())
    meta["user_name"] = "Samuel Chibuzor Asagwara"
    ctx = ls._build_memory_context(meta)
    assert "Samuel Chibuzor Asagwara" in ctx, (
        "memory was loaded but nothing in it reaches the model")


def test_the_cycle_is_broken_deliberately_and_says_so():
    """A bare `import nova` with no explanation is the kind of line someone
    tidies away, and the bug comes straight back."""
    src = inspect.getsource(ls._import_memory)
    assert "import nova" in src
    assert "do not remove" in src.lower()


def test_losing_memory_is_reported_rather_than_swallowed():
    """Amnesia that announces itself is a bug report. Amnesia that does not is
    NOVA seeming not to know someone she has known for weeks."""
    for fn in (ls._load_meta, ls._build_memory_context):
        src = inspect.getsource(fn)
        assert "_log(" in src, f"{fn.__name__} still fails silently"


# ── stated identity, not recalled identity ───────────────────────────────────

def test_identity_is_stated_to_the_model(monkeypatch):
    from desk import settings as s
    monkeypatch.setattr(s, "get", lambda k, d=None: {
        "user_name": "Psalms",
        "user_role": "the person who created NOVA",
    }.get(k, d))

    block = ls._identity_block()
    assert "Psalms" in block
    assert "created NOVA" in block
    assert "do not ask their name" in block.lower(), (
        "nothing stops her asking again, which is the whole complaint")


def test_a_placeholder_name_is_not_treated_as_an_answer(monkeypatch):
    """"User" is the shipped default. Greeting someone as User is worse than
    admitting you do not know them."""
    from desk import settings as s
    for placeholder in ("User", "", "unknown"):
        monkeypatch.setattr(s, "get", lambda k, d=None, p=placeholder: {
            "user_name": p, "user_role": "the boss"}.get(k, d))
        assert ls._identity_block() == ""


def test_a_name_without_a_role_still_identifies_them(monkeypatch):
    from desk import settings as s
    monkeypatch.setattr(s, "get", lambda k, d=None: {
        "user_name": "Ada", "user_role": ""}.get(k, d))
    block = ls._identity_block()
    assert "Ada" in block and "do not ask" in block.lower()


def test_identity_outranks_recalled_memory():
    """Memory is what NOVA picked up; identity is what she was told. When they
    disagree — and here they did, five ways — the telling wins."""
    src = inspect.getsource(ls.LiveManager._connect_and_run)
    assert src.index("_identity_block()") < src.index("[BACKGROUND MEMORY]")
