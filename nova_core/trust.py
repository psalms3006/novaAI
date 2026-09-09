"""nova_core.trust — how NOVA knows where an instruction came from.

Trust is ambient, not a parameter. Once NOVA has read a web page, everything
it does for the rest of that turn is potentially influenced by what the page
said, and threading a `trust=` argument through every call site would mean one
missed hand-off silently restores full privilege.

A ContextVar carries it instead, so entering untrusted content taints the
scope and leaving it restores what was there before. It is async-safe, which
matters because the Live voice path runs inside asyncio.

    with untrusted("web:example.com"):
        summary = summarise(page_text)     # any tool call in here is gated
"""
from __future__ import annotations

import contextlib
import contextvars
from typing import Iterator

from .permissions import Trust

_current: contextvars.ContextVar[Trust] = contextvars.ContextVar(
    "nova_trust", default=Trust.USER)
_source: contextvars.ContextVar[str] = contextvars.ContextVar(
    "nova_trust_source", default="")


def current_trust() -> Trust:
    return _current.get()


def current_source() -> str:
    """What tainted the current scope, for the audit trail and for telling the
    user *why* NOVA is asking."""
    return _source.get()


@contextlib.contextmanager
def untrusted(source: str = "") -> Iterator[None]:
    """Mark everything inside as influenced by content NOVA did not author."""
    tok, tok_src = _current.set(Trust.UNTRUSTED), _source.set(source)
    try:
        yield
    finally:
        _current.reset(tok)
        _source.reset(tok_src)


@contextlib.contextmanager
def as_trust(trust: Trust, source: str = "") -> Iterator[None]:
    tok, tok_src = _current.set(trust), _source.set(source)
    try:
        yield
    finally:
        _current.reset(tok)
        _source.reset(tok_src)


#: Tools whose *results* are content NOVA did not author. After one of these
#: runs, the rest of the turn is reasoning over text an outsider may control.
TAINTING_TOOLS = frozenset({
    "web_search", "fetch_url", "browser_read", "browser_control",
    "file_processor", "read_file", "file_read", "knowledge_search",
    "zim_search", "rag_search", "rag_add", "document_search",
})


def taints(tool: str) -> bool:
    return (tool or "") in TAINTING_TOOLS


__all__ = ["current_trust", "current_source", "untrusted", "as_trust",
           "TAINTING_TOOLS", "taints", "Trust"]
