"""nova_cloud.api_telemetry — batched, non-blocking event ingest.

    POST /v1/telemetry/events   {"events": [...]}

The client fires these from a background queue and never waits on the
response, so this endpoint's job is to accept a batch quickly, sanitise it
hard, and get out of the way. It returns counts rather than per-event errors:
a client must never retry-loop because one malformed event was refused.

Honouring the user's choice happens here as well as on the client. If the
account has telemetry disabled, the batch is acknowledged and discarded --
a privacy control that only worked when the client felt like it would not be a
privacy control.
"""
from __future__ import annotations

from flask import Blueprint, g, jsonify, request

from .auth_guard import user_required
from .db import session_scope
from .models import Preference
from .telemetry_sink import (
    record_agent_run, record_error, record_event, record_model_call,
)

bp = Blueprint("telemetry", __name__, url_prefix="/v1/telemetry")

_MAX_BATCH = 200


def _telemetry_enabled(s, user_id: str) -> bool:
    row = s.get(Preference, {"user_id": user_id, "key": "telemetry_enabled"})
    if row is None:
        return True                      # default on, and disclosed at setup
    return bool(row.value)


@bp.post("/events")
@user_required
def ingest():
    body = request.get_json(silent=True) or {}
    events = body.get("events")
    if not isinstance(events, list):
        return jsonify({"ok": False, "error": "bad_request",
                        "message": "events must be a list"}), 400

    accepted = dropped = 0
    with session_scope() as s:
        if not _telemetry_enabled(s, g.user_id):
            return jsonify({"ok": True, "accepted": 0, "dropped": len(events),
                            "reason": "telemetry_disabled"})

        for ev in events[:_MAX_BATCH]:
            if not isinstance(ev, dict):
                dropped += 1
                continue
            kind = str(ev.get("kind") or "activity")
            try:
                if kind == "model_call":
                    record_model_call(
                        s, user_id=g.user_id, device_id=g.device_id,
                        provider=ev.get("provider") or "unknown",
                        model=ev.get("model") or "unknown",
                        latency_ms=_int(ev.get("latency_ms")),
                        first_token_ms=_int(ev.get("first_token_ms")),
                        status=ev.get("status") or "success",
                        error_code=ev.get("error_code"),
                        tokens_in=_int(ev.get("tokens_in")),
                        tokens_out=_int(ev.get("tokens_out")),
                        offline=bool(ev.get("offline")),
                    )
                elif kind == "agent_run":
                    record_agent_run(
                        s, user_id=g.user_id, device_id=g.device_id,
                        agent=ev.get("agent") or "unknown",
                        task_type=ev.get("task_type"),
                        started_at=_float(ev.get("started_at")),
                        ended_at=_float(ev.get("ended_at")),
                        duration_ms=_int(ev.get("duration_ms")),
                        status=ev.get("status") or "completed",
                        model=ev.get("model"),
                        error_code=ev.get("error_code"),
                    )
                elif kind == "error":
                    record_error(
                        s, user_id=g.user_id, device_id=g.device_id,
                        code=ev.get("code") or "UNKNOWN",
                        app_version=ev.get("app_version"),
                        platform=ev.get("platform"),
                        context=ev.get("context") if isinstance(
                            ev.get("context"), dict) else None,
                    )
                else:
                    attrs = ev.get("attrs") if isinstance(ev.get("attrs"), dict) else {}
                    record_event(s, str(ev.get("type") or "UNKNOWN"),
                                 user_id=g.user_id, device_id=g.device_id, **attrs)
                accepted += 1
            except Exception:
                # One bad event must never fail a batch.
                dropped += 1
        if len(events) > _MAX_BATCH:
            dropped += len(events) - _MAX_BATCH

    return jsonify({"ok": True, "accepted": accepted, "dropped": dropped})


def _int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
