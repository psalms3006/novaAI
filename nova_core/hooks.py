"""nova_core.hooks — code that runs around every tool call.

Every tool call NOVA makes, from voice, chat, a background task or a skill,
passes through one dispatcher (`nova._execute_tool_sync`). Hooks attach there:

    pre   fn(tool, args, meta) -> None | {"deny": reason} | {"args": new_args}
    post  fn(tool, args, result, meta) -> None | new_result (str)

Ordering is the safety property. Pre-hooks run *before* NOVA's permission
check and confirmation gate, so a hook may refuse a call or narrow its
arguments, and whatever it passes on is still checked. No hook can turn a
refusal from the permission layer into an allow -- there is no "allow"
decision to give.

A hook that raises is logged and skipped (fail-open), because a broken
convenience hook must not stop NOVA working; a hook registered with
`critical=True` fails closed instead, for hooks whose job is to prevent
something.

These are in-process Python hooks registered by NOVA's own modules. There are
deliberately no user-configured shell-command hooks: running arbitrary
commands around every tool call is exactly the foothold NOVA refuses to give
third-party content elsewhere.
"""
from __future__ import annotations

import fnmatch
import logging
import threading
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("nova.hooks")


@dataclass
class _Hook:
    fn: Callable
    matcher: str
    critical: bool
    name: str


_lock = threading.Lock()
_pre: list = []
_post: list = []


def _matches(pattern: str, tool: str) -> bool:
    return any(fnmatch.fnmatchcase(tool, p.strip()) for p in (pattern or "*").split("|"))


def add_pre(fn: Callable, matcher: str = "*", *, critical: bool = False, name: str = "") -> Callable:
    with _lock:
        _pre.append(_Hook(fn, matcher, critical, name or getattr(fn, "__name__", "hook")))
    return fn


def add_post(fn: Callable, matcher: str = "*", *, critical: bool = False, name: str = "") -> Callable:
    with _lock:
        _post.append(_Hook(fn, matcher, critical, name or getattr(fn, "__name__", "hook")))
    return fn


def remove(fn: Callable) -> None:
    with _lock:
        _pre[:] = [h for h in _pre if h.fn is not fn]
        _post[:] = [h for h in _post if h.fn is not fn]


def clear() -> None:
    with _lock:
        _pre.clear()
        _post.clear()


def run_pre(tool: str, args: dict, meta: dict) -> tuple:
    """(allowed, args, reason). Stops at the first hook that denies."""
    with _lock:
        hooks = [h for h in _pre if _matches(h.matcher, tool)]
    for h in hooks:
        try:
            out = h.fn(tool, dict(args or {}), meta)
        except Exception as e:
            if h.critical:
                log.error("critical pre-hook %s failed; refusing %s: %s", h.name, tool, e)
                return False, args, f"a safety check ({h.name}) could not run"
            log.warning("pre-hook %s failed for %s (ignored): %s", h.name, tool, e)
            continue
        if not out:
            continue
        if out.get("deny"):
            return False, args, str(out["deny"])
        if isinstance(out.get("args"), dict):
            args = out["args"]
    return True, args, ""


def run_post(tool: str, args: dict, result: str, meta: dict) -> str:
    with _lock:
        hooks = [h for h in _post if _matches(h.matcher, tool)]
    for h in hooks:
        try:
            out = h.fn(tool, args, result, meta)
        except Exception as e:
            if h.critical:
                log.error("critical post-hook %s failed for %s: %s", h.name, tool, e)
                return f"The result of {tool} was withheld: a safety check ({h.name}) could not run."
            log.warning("post-hook %s failed for %s (ignored): %s", h.name, tool, e)
            continue
        if isinstance(out, str):
            result = out
    return result


def registered() -> dict:
    with _lock:
        return {"pre": [(h.name, h.matcher) for h in _pre], "post": [(h.name, h.matcher) for h in _post]}


__all__ = ["add_pre", "add_post", "remove", "clear", "run_pre", "run_post", "registered"]
