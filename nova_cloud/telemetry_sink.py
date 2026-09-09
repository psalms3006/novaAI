"""nova_cloud.telemetry_sink — writing operational events, safely.

The one rule this module exists to enforce: **operational metadata in, private
content never**. Clients are not trusted to respect that, so every attribute
that reaches an event row is filtered here by an allow-list of keys and a
scalar-only, length-capped value check.

If a client sends `{"transcript": "..."}` or `{"prompt": "..."}`, it is
dropped. Not truncated, not hashed -- dropped.
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from .models import ActivityEvent, AgentRun, ErrorEvent, ModelCall, now

# Event types the platform understands. An unknown type is stored as
# "UNKNOWN" rather than rejected, so an older backend never silently loses a
# newer client's activity -- but it cannot invent new indexed cardinality.
KNOWN_EVENTS = {
    "USER_CREATED", "USER_LOGIN", "USER_LOGOUT", "EMAIL_VERIFIED",
    "PASSWORD_RESET", "AUTH_FAILURE",
    "DEVICE_REGISTERED", "DEVICE_REVOKED", "DEVICE_RENAMED",
    "NOVA_STARTED", "NOVA_STOPPED",
    "SESSION_STARTED", "SESSION_ENDED",
    "VOICE_SESSION_STARTED", "VOICE_SESSION_ENDED",
    "TASK_STARTED", "TASK_COMPLETED", "TASK_FAILED",
    "AGENT_STARTED", "AGENT_COMPLETED",
    "NETWORK_DEGRADED", "OFFLINE_FALLBACK", "ONLINE_RECOVERED",
    "PREFERENCES_SYNCED", "ACCOUNT_DELETED",
}

# The error taxonomy. A defined set beats free-form strings: it keeps the
# admin Errors page groupable, makes "did this start after 0.3.1?" answerable,
# and stops one client's typo fragmenting a real problem across ten rows.
# An unrecognised code is stored under UNKNOWN_ERROR with the original kept in
# context, so a newer client is never silently lost.
ERROR_CODES = {
    # authentication and account
    "AUTH_ERROR", "AUTH_FAILURE", "SESSION_EXPIRED", "DEVICE_REVOKED",
    # platform
    "DATABASE_ERROR", "SYNC_ERROR", "NETWORK_ERROR", "EMAIL_DELIVERY_FAILED",
    # runtime
    "VOICE_ERROR", "VOICE_MICROPHONE_ERROR", "VOICE_SPEAKER_ERROR",
    "MODEL_ERROR", "MODEL_TIMEOUT", "MODEL_UNAVAILABLE",
    "AGENT_ERROR", "AGENT_FAILURE", "TOOL_ERROR", "TOOL_FAILURE",
    "MEMORY_ERROR", "KNOWLEDGE_ERROR",
    # catch-all
    "UNKNOWN_ERROR",
}

# Attribute keys accepted on an activity event. Anything else is discarded.
ALLOWED_ATTRS = {
    "platform", "app_version", "reason", "scope", "sessions", "provider",
    "model", "latency_ms", "status", "task_type", "agent", "duration_ms",
    "error_code", "offline", "count", "trigger", "surface", "source",
}

# Keys that must never be stored, even if someone adds them to ALLOWED_ATTRS by
# mistake. Belt and braces, because this is the failure that matters most.
FORBIDDEN_ATTRS = re.compile(
    r"(transcript|prompt|completion|message|content|text|audio|recording|"
    r"conversation|utterance|query|response_body|password|token|secret|api_key)",
    re.I,
)

_MAX_STR = 120
_MAX_ATTRS = 12


def sanitise_attrs(attrs: dict[str, Any] | None) -> dict[str, Any]:
    """Allow-list, scalar-only, length-capped."""
    if not attrs:
        return {}
    out: dict[str, Any] = {}
    for k, v in attrs.items():
        if len(out) >= _MAX_ATTRS:
            break
        key = str(k)[:32]
        if key not in ALLOWED_ATTRS or FORBIDDEN_ATTRS.search(key):
            continue
        if isinstance(v, bool) or isinstance(v, int) or isinstance(v, float):
            out[key] = v
        elif isinstance(v, str):
            s = v[:_MAX_STR]
            # A value that looks like free-form prose is content, not metadata.
            if len(s.split()) > 12:
                continue
            out[key] = s
        # Anything structured (dict/list) is refused: that is where content
        # hides.
    return out


def record_event(session: Session, event_type: str, *, user_id: str | None = None,
                 device_id: str | None = None, **attrs) -> ActivityEvent:
    """Append one activity event. Never raises into the caller's flow."""
    etype = event_type if event_type in KNOWN_EVENTS else "UNKNOWN"
    clean = sanitise_attrs(attrs)
    ev = ActivityEvent(
        user_id=user_id,
        device_id=device_id,
        type=etype,
        ts=now(),
        app_version=clean.pop("app_version", None),
        platform=clean.pop("platform", None),
        attrs=clean or None,
    )
    session.add(ev)
    return ev


def record_model_call(session: Session, *, user_id: str | None, device_id: str | None,
                      provider: str, model: str, latency_ms: int | None = None,
                      first_token_ms: int | None = None, status: str = "success",
                      error_code: str | None = None, tokens_in: int | None = None,
                      tokens_out: int | None = None, offline: bool = False) -> ModelCall:
    row = ModelCall(
        user_id=user_id, device_id=device_id,
        provider=str(provider)[:48], model=str(model)[:96],
        latency_ms=latency_ms, first_token_ms=first_token_ms,
        status=str(status)[:16], error_code=(str(error_code)[:48] if error_code else None),
        tokens_in=tokens_in, tokens_out=tokens_out, offline=bool(offline),
    )
    session.add(row)
    return row


def record_agent_run(session: Session, *, user_id: str | None, device_id: str | None,
                     agent: str, task_type: str | None = None,
                     started_at: float | None = None, ended_at: float | None = None,
                     duration_ms: int | None = None, status: str = "running",
                     model: str | None = None,
                     error_code: str | None = None) -> AgentRun:
    row = AgentRun(
        user_id=user_id, device_id=device_id, agent=str(agent)[:64],
        task_type=(str(task_type)[:64] if task_type else None),
        started_at=started_at or now(), ended_at=ended_at,
        duration_ms=duration_ms, status=str(status)[:16],
        model=(str(model)[:96] if model else None),
        error_code=(str(error_code)[:48] if error_code else None),
    )
    session.add(row)
    return row


def record_error(session: Session, *, user_id: str | None, device_id: str | None,
                 code: str, app_version: str | None = None,
                 platform: str | None = None,
                 context: dict | None = None) -> ErrorEvent:
    raw = str(code)[:48]
    known = raw in ERROR_CODES
    ctx = dict(context or {})
    if not known:
        # Keep what the client actually said, but do not let it create a new
        # indexed category.
        ctx["source"] = raw[:40]
    row = ErrorEvent(
        user_id=user_id, device_id=device_id,
        code=raw if known else "UNKNOWN_ERROR",
        app_version=(str(app_version)[:32] if app_version else None),
        platform=(str(platform)[:32] if platform else None),
        context=sanitise_attrs(ctx) or None,
    )
    session.add(row)
    return row


__all__ = ["KNOWN_EVENTS", "ERROR_CODES", "ALLOWED_ATTRS", "sanitise_attrs",
           "record_event", "record_model_call", "record_agent_run",
           "record_error"]
