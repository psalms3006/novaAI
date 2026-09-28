"""nova_cloud.api_sync — cloud-synced preferences, profile and privacy.

    GET   /v1/sync/preferences?since=<ts>   changed preferences
    POST  /v1/sync/preferences              push local changes
    GET   /v1/sync/profile
    PATCH /v1/sync/profile
    GET   /v1/sync/flags                    feature flags for this user
    POST  /v1/account/delete                delete the account

Only keys in SYNCED_KEYS ever cross the network. Everything hardware-shaped --
microphone, speaker, model paths, ZIM files, local permissions -- stays on the
device it belongs to, because syncing it would actively break the other
machine.

Conflicts resolve last-write-wins on the client-supplied `updated_at`, which is
right for scalar preferences: the newest thing the human chose is what they
want. The device that wrote it is recorded so the history is explainable.
"""
from __future__ import annotations

from flask import Blueprint, g, jsonify, request
from sqlalchemy import select

from .auth_guard import user_required
from .db import session_scope
from .models import (
    AuthSession, Device, FeatureFlag, FeatureFlagOverride, Preference, Profile,
    User, now,
)
from .telemetry_sink import record_event

bp = Blueprint("sync", __name__)

# Preferences that describe the *person*, so they should follow them between
# machines.
SYNCED_KEYS = {
    "theme", "accent", "density", "animations",
    "response_style", "history_turns", "show_tool_activity",
    "voice_responses", "continuous_conversation", "barge_in",
    "memory_enabled", "user_system_prompt", "locale",
    "quiet_hours_start", "quiet_hours_end", "proactive_enabled",
    "telemetry_enabled", "cloud_sync_enabled", "personalisation_enabled",
}

# Preferences that describe the *machine*. Named explicitly so the split is
# documented in code, not just in prose.
DEVICE_LOCAL_KEYS = {
    "input_device", "output_device", "mic_gain", "speaker_volume",
    "ollama_url", "local_model", "zim_path", "maps_path", "download_dir",
    "permissions", "developer_mode", "window_bounds", "ambient_position",
}

_MAX_VALUE_CHARS = 4000


def _clean_value(v):
    """Preferences are settings, not a document store."""
    if isinstance(v, (bool, int, float)) or v is None:
        return v
    if isinstance(v, str):
        return v[:_MAX_VALUE_CHARS]
    raise ValueError("preference values must be scalar")


@bp.get("/v1/sync/preferences")
@user_required
def get_preferences():
    try:
        since = float(request.args.get("since", "0") or 0)
    except ValueError:
        since = 0.0
    with session_scope() as s:
        rows = s.scalars(select(Preference).where(
            Preference.user_id == g.user_id,
            Preference.updated_at > since)).all()
        return jsonify({
            "ok": True,
            "server_time": now(),
            "preferences": {
                r.key: {"value": r.value, "updated_at": r.updated_at,
                        "updated_by_device": r.updated_by_device,
                        "version": r.version}
                for r in rows
            },
        })


@bp.post("/v1/sync/preferences")
@user_required
def put_preferences():
    body = request.get_json(silent=True) or {}
    incoming = body.get("preferences") or {}
    if not isinstance(incoming, dict):
        return jsonify({"ok": False, "error": "bad_request",
                        "message": "preferences must be an object"}), 400

    applied, rejected, conflicts = [], [], []
    with session_scope() as s:
        for key, payload in list(incoming.items())[:100]:
            k = str(key)[:64]
            if k not in SYNCED_KEYS:
                # Device-local or unknown: refused, and the client is told so
                # rather than being left believing it synced.
                rejected.append(k)
                continue
            if isinstance(payload, dict):
                value = payload.get("value")
                ts = float(payload.get("updated_at") or now())
            else:
                value, ts = payload, now()
            try:
                value = _clean_value(value)
            except ValueError:
                rejected.append(k)
                continue

            row = s.get(Preference, {"user_id": g.user_id, "key": k})
            if row is None:
                s.add(Preference(user_id=g.user_id, key=k, value=value,
                                 updated_at=ts, updated_by_device=g.device_id,
                                 version=1))
                applied.append(k)
            elif ts >= row.updated_at:
                row.value = value
                row.updated_at = ts
                row.updated_by_device = g.device_id
                row.version = int(row.version) + 1
                applied.append(k)
            else:
                # The server holds something newer. Report it so the client can
                # adopt the winner instead of silently diverging.
                conflicts.append({"key": k, "server_value": row.value,
                                  "server_updated_at": row.updated_at})
        if applied:
            record_event(s, "PREFERENCES_SYNCED", user_id=g.user_id,
                         device_id=g.device_id, count=len(applied))
    return jsonify({"ok": True, "applied": applied, "rejected": rejected,
                    "conflicts": conflicts, "server_time": now()})


@bp.get("/v1/sync/profile")
@user_required
def get_profile():
    with session_scope() as s:
        p = s.get(Profile, g.user_id)
        u = s.get(User, g.user_id)
        return jsonify({"ok": True, "profile": {
            "display_name": p.display_name if p else "",
            "locale": p.locale if p else "en",
            "avatar_url": p.avatar_url if p else None,
            "email": u.email if u else "",
            "created_at": u.created_at if u else None,
        }})


@bp.patch("/v1/sync/profile")
@user_required
def patch_profile():
    body = request.get_json(silent=True) or {}
    with session_scope() as s:
        p = s.get(Profile, g.user_id)
        if p is None:
            p = Profile(user_id=g.user_id)
            s.add(p)
        if "display_name" in body:
            p.display_name = str(body["display_name"] or "").strip()[:120]
        if "locale" in body:
            p.locale = str(body["locale"] or "en").strip()[:16]
        return jsonify({"ok": True, "profile": {
            "display_name": p.display_name, "locale": p.locale}})


@bp.get("/v1/sync/flags")
@user_required
def get_flags():
    """Server-controlled flags. The client caches these so a flag lookup never
    requires the network."""
    import hashlib
    with session_scope() as s:
        flags = s.scalars(select(FeatureFlag)).all()
        overrides = {
            o.flag_key: o.enabled for o in s.scalars(
                select(FeatureFlagOverride).where(
                    FeatureFlagOverride.user_id == g.user_id)).all()
        }
        out = {}
        for f in flags:
            if f.key in overrides:
                out[f.key] = bool(overrides[f.key])
                continue
            if f.enabled:
                out[f.key] = True
            elif f.rollout_percent > 0:
                # Stable per-user bucketing: a user does not flip between
                # builds or restarts.
                h = hashlib.sha256(f"{f.key}:{g.user_id}".encode()).digest()
                bucket = int.from_bytes(h[:4], "big") % 100
                out[f.key] = bucket < f.rollout_percent
            else:
                out[f.key] = False
        return jsonify({"ok": True, "flags": out, "server_time": now()})


@bp.post("/v1/account/delete")
@user_required(allow_unverified=True)
def delete_account():
    """Delete the account and everything personally attached to it.

    Deliberate: requires the current password. Telemetry rows are
    de-identified rather than dropped, so platform-level counts stay honest
    while nothing remains linked to the person.
    """
    from . import security as sec
    body = request.get_json(silent=True) or {}
    password = body.get("password") or ""
    if (body.get("confirm") or "") != "DELETE":
        return jsonify({"ok": False, "error": "confirmation_required",
                        "message": 'Send {"confirm": "DELETE"} to proceed.'}), 400

    with session_scope() as s:
        u = s.get(User, g.user_id)
        if u is None:
            return jsonify({"ok": False, "error": "not_found"}), 404
        if not sec.verify_password(u.password_hash, password):
            return jsonify({"ok": False, "error": "invalid_credentials",
                            "message": "Incorrect password."}), 401

        from .models import ActivityEvent, AgentRun, ErrorEvent, ModelCall
        for model in (ActivityEvent, ModelCall, AgentRun, ErrorEvent):
            for row in s.scalars(select(model).where(model.user_id == u.id)).all():
                row.user_id = None
                row.device_id = None

        for a in s.scalars(select(AuthSession).where(
                AuthSession.user_id == u.id)).all():
            s.delete(a)
        for d in s.scalars(select(Device).where(Device.user_id == u.id)).all():
            s.delete(d)
        for p in s.scalars(select(Preference).where(
                Preference.user_id == u.id)).all():
            s.delete(p)
        # The profile is removed by the cascade on users; deleting it here as
        # well makes SQLAlchemy warn that the row it expected was already gone.
        record_event(s, "ACCOUNT_DELETED")
        s.delete(u)
    return jsonify({"ok": True, "message": "Account deleted."})
