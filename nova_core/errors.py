"""nova_core.errors — what kind of failure is this, and what should happen next?

A timeout and a revoked key both arrive as "it didn't work", but they call for
opposite responses: wait and retry, versus ask the person to reconnect. Treating
them alike is how a skill that hit one slow response got marked broken, or
how NOVA retried a refused request three times.

`classify` takes an exception, an HTTP status, or the text a tool returned, and
answers with one of six classes. Unknown shapes are INTERNAL -- never guessed
into a class that would trigger a retry.
"""
from __future__ import annotations

import enum
import re
from typing import Optional


class ErrorClass(str, enum.Enum):
    TRANSIENT = "transient"          # timeout, rate limit, 5xx, dropped connection: retry later
    AUTH = "auth"                    # expired/revoked credential: the person must reconnect
    PERMISSION = "permission"        # NOVA's policy or the person said no: do not retry
    INVALID_INPUT = "invalid_input"  # wrong arguments: fix the call, don't retry it as-is
    UNAVAILABLE = "unavailable"      # not installed, offline, not connected: needs setup
    INTERNAL = "internal"            # a bug or something unrecognised


_TEXT_RULES = [
    (ErrorClass.PERMISSION, r"^refused\b|\bdeclined\b|\bnot permitted\b|\bnot allowed\b|"
                            r"\bdid not run\b.*\bbackground task\b|\buser said no\b"),
    (ErrorClass.AUTH, r"\b(401|403)\b|unauthori[sz]ed|forbidden|invalid api key|api key not valid|"
                      r"\bauth_required\b|needs to be reconnected|expired token|token expired"),
    (ErrorClass.TRANSIENT, r"\b(429|500|502|503|504)\b|timed? ?out|timeout|rate.?limit|"
                           r"temporarily|try again|connection (reset|aborted|refused)|"
                           r"resource.?exhausted|overloaded|unavailable \(503\)"),
    (ErrorClass.UNAVAILABLE, r"\bunavailable\b|not installed|module missing|no module named|"
                             r"not connected|offline|no internet|could not resolve|getaddrinfo"),
    (ErrorClass.INVALID_INPUT, r"\bmissing\b.*\b(argument|parameter|field)\b|\binvalid\b|"
                               r"\brequired\b|\bunknown (command|action)\b|\bbad request\b|\b400\b"),
]
_COMPILED = [(c, re.compile(p, re.I)) for c, p in _TEXT_RULES]


def from_status(status: int) -> Optional[ErrorClass]:
    if status in (401, 403):
        return ErrorClass.AUTH
    if status == 429 or 500 <= status < 600:
        return ErrorClass.TRANSIENT
    if status in (400, 404, 405, 409, 413, 422):
        return ErrorClass.INVALID_INPUT
    return None


def classify(err=None, *, status: Optional[int] = None) -> ErrorClass:
    if status is not None:
        c = from_status(int(status))
        if c:
            return c
    if isinstance(err, BaseException):
        if isinstance(err, PermissionError):
            text = str(err)
            return ErrorClass.AUTH if re.search(r"auth|reconnect|credential", text, re.I) \
                else ErrorClass.PERMISSION
        if isinstance(err, (TimeoutError, ConnectionError)):
            return ErrorClass.TRANSIENT
        if isinstance(err, (ModuleNotFoundError, FileNotFoundError)):
            return ErrorClass.UNAVAILABLE
        if isinstance(err, (ValueError, TypeError, KeyError)):
            return ErrorClass.INVALID_INPUT
        name = type(err).__name__.lower()
        if "timeout" in name or "connection" in name:
            return ErrorClass.TRANSIENT
        err = f"{type(err).__name__}: {err}"
    text = str(err or "").strip()
    for cls, rx in _COMPILED:
        if rx.search(text):
            return cls
    return ErrorClass.INTERNAL


def retryable(c: ErrorClass) -> bool:
    return c is ErrorClass.TRANSIENT


_ADVICE = {
    ErrorClass.TRANSIENT: "It looks temporary; trying again shortly should work.",
    ErrorClass.AUTH: "The connection needs to be renewed in NOVA's settings.",
    ErrorClass.PERMISSION: "That was refused, so it should not be retried as it is.",
    ErrorClass.INVALID_INPUT: "The request itself was wrong and needs changing before trying again.",
    ErrorClass.UNAVAILABLE: "Something it needs isn't set up or reachable on this computer.",
    ErrorClass.INTERNAL: "Something unexpected went wrong.",
}


def advice(c: ErrorClass) -> str:
    return _ADVICE[c]


#: How a tool says it did not work. Only these are logged: successful results
#: can hold the person's content, and the log is for finding faults.
_FAILURE_OPENERS = re.compile(
    r"^(error|failed|refused|action cancelled|i can'?t|i couldn'?t|could not|couldn'?t|"
    r"unable to|tool '[^']+' (is unavailable|error|encountered)|the '[^']+' tool did not|"
    r"no confirmation received|permission denied|traceback|[a-z_]+ error:)", re.I)


def looks_failed(result) -> bool:
    return isinstance(result, str) and bool(_FAILURE_OPENERS.match(result.strip()))


def log_failures(tool: str, args: dict, result, meta: dict):
    """Post-hook: leave a trail when a tool fails, so the next look at the log
    shows what went wrong -- not just "tool X finished in 0.1s"."""
    if looks_failed(result):
        import logging
        c = classify(result)
        logging.getLogger("nova.tools").warning(
            "[TOOL] %s failed (%s): %s", tool, c.value, result.strip().replace("\n", " ")[:160])
    return None


__all__ = ["ErrorClass", "classify", "from_status", "retryable", "advice",
           "looks_failed", "log_failures"]
