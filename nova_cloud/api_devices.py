"""nova_cloud.api_devices — the user's own device management.

    GET    /v1/devices              list this account's devices
    PATCH  /v1/devices/<id>         rename
    POST   /v1/devices/<id>/revoke  sign that device out

Scoping is by `g.user_id` from the token, so a device id belonging to another
account is simply not found -- there is no path here that reads a device the
caller does not own.
"""
from __future__ import annotations

from flask import Blueprint, g, jsonify, request
from sqlalchemy import select

from .auth_guard import invalidate_auth_cache, user_required
from .db import session_scope
from .models import AuthSession, Device, now
from .telemetry_sink import record_event

bp = Blueprint("devices", __name__, url_prefix="/v1/devices")


def _shape(d: Device, current_device_id: str) -> dict:
    return {
        "id": d.id,
        "name": d.name,
        "platform": d.platform,
        "app_version": d.app_version,
        "created_at": d.created_at,
        "last_seen_at": d.last_seen_at,
        "revoked": d.revoked_at is not None,
        "current": d.id == current_device_id,
    }


@bp.get("")
@user_required
def list_devices():
    with session_scope() as s:
        rows = s.scalars(
            select(Device).where(Device.user_id == g.user_id)
            .order_by(Device.last_seen_at.desc().nullslast())
        ).all()
        return jsonify({"ok": True,
                        "devices": [_shape(d, g.device_id) for d in rows]})


@bp.patch("/<device_id>")
@user_required
def rename_device(device_id: str):
    name = ((request.get_json(silent=True) or {}).get("name") or "").strip()[:120]
    if not name:
        return jsonify({"ok": False, "error": "bad_request",
                        "message": "A device name is required."}), 400
    with session_scope() as s:
        d = s.get(Device, device_id)
        if d is None or d.user_id != g.user_id:
            return jsonify({"ok": False, "error": "not_found",
                            "message": "Device not found."}), 404
        d.name = name
        record_event(s, "DEVICE_RENAMED", user_id=g.user_id, device_id=d.id)
        return jsonify({"ok": True, "device": _shape(d, g.device_id)})


@bp.post("/<device_id>/revoke")
@user_required
def revoke_device(device_id: str):
    with session_scope() as s:
        d = s.get(Device, device_id)
        if d is None or d.user_id != g.user_id:
            return jsonify({"ok": False, "error": "not_found",
                            "message": "Device not found."}), 404
        d.revoked_at = now()
        n = 0
        for a in s.scalars(select(AuthSession).where(
                AuthSession.device_id == d.id,
                AuthSession.revoked_at.is_(None))).all():
            a.revoked_at = now()
            n += 1
        record_event(s, "DEVICE_REVOKED", user_id=g.user_id, device_id=d.id,
                     sessions=n, source="user")
    # Take effect now rather than when the guard cache next expires.
    invalidate_auth_cache(g.user_id)
    return jsonify({"ok": True, "sessions_revoked": n})
