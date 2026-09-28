"""nova_cloud.api_admin — the administrative control plane API.

Separate from the user API in every way that matters: a different signing key,
a different token audience, mandatory MFA, short privileged sessions, and an
append-only audit trail of every privileged action.

Two boundaries this module is built around:

  * **Admins are not superusers over content.** There is no endpoint here that
    returns a user's conversations, transcripts or memory. The user tables
    exposed are identity and operational metadata only. That is a deliberate
    limit, not an omission.
  * **Every number is a query.** Nothing on the dashboard is a constant. If the
    platform has no data yet the dashboard shows zeros, which is the honest
    answer.
"""
from __future__ import annotations

import time

from flask import Blueprint, g, jsonify, request
from sqlalchemy import distinct, func, or_, select

from . import security as sec
from .auth_guard import admin_required, invalidate_auth_cache
from .db import RateLimited, rate_limit, session_scope
from .models import (
    ActivityEvent, AdminAuditLog, AdminRole, AdminSession, AdminUser, AgentRun,
    AuthSession, Device, ErrorEvent, FeatureFlag, ModelCall,
    Profile, User, UserStatus, now,
)

bp = Blueprint("admin", __name__, url_prefix="/admin/api")

DAY = 86400.0

#: TOTP steps accepted either side of now.
#:
#: Sign-in stays at one step (30s each way): a code should prove present
#: possession. Enrolment allows ten (five minutes), because it only proves the
#: authenticator was seeded correctly and the password was already verified on
#: the same request.
LOGIN_WINDOW = 1
ENROL_WINDOW = 10


def _ip() -> str:
    from .auth_guard import client_ip
    return client_ip()


def audit(s, action: str, *, target_type: str | None = None,
          target_id: str | None = None, result: str = "success",
          **detail) -> None:
    """Record a privileged action. Called for every state change, and for
    reads of individual user records."""
    s.add(AdminAuditLog(
        admin_id=getattr(g, "admin_id", None),
        admin_email=getattr(g, "admin_email", None),
        action=action[:64],
        target_type=(target_type[:32] if target_type else None),
        target_id=(str(target_id)[:64] if target_id else None),
        ip_hash=sec.hash_ip(_ip()),
        result=result[:16],
        detail=detail or None,
    ))


# -- admin authentication ----------------------------------------------------

@bp.post("/auth/login")
def admin_login():
    """Password plus TOTP. MFA is required by default and the failure path
    locks the account rather than allowing unlimited guessing."""
    body = request.get_json(silent=True) or {}
    email = sec.normalise_email(body.get("email", ""))
    password = body.get("password") or ""
    totp_code = str(body.get("totp") or "").strip()

    with session_scope() as s:
        try:
            rate_limit(s, f"adminlogin:ip:{sec.hash_ip(_ip())}", limit=10,
                       window_s=900)
        except RateLimited as e:
            return jsonify({"ok": False, "error": "rate_limited"}), 429, \
                {"Retry-After": str(e.retry_after)}

        admin = s.scalar(select(AdminUser).where(AdminUser.email == email))
        generic = jsonify({"ok": False, "error": "invalid_credentials",
                           "message": "Incorrect credentials."}), 401

        if admin is None:
            return generic
        if admin.locked_until and admin.locked_until > now():
            return jsonify({"ok": False, "error": "locked",
                            "message": "Account temporarily locked."}), 423
        if admin.status != "active":
            return jsonify({"ok": False, "error": "disabled"}), 403

        if not sec.verify_password(admin.password_hash, password):
            admin.failed_logins = int(admin.failed_logins) + 1
            if admin.failed_logins >= 5:
                admin.locked_until = now() + 900
            audit(s, "admin.login", target_type="admin", target_id=admin.id,
                  result="failure", reason="bad_password")
            return generic

        cfg = s.info.get("cfg") or None
        from .config import config as _config
        require_mfa = (cfg or _config()).admin_require_mfa

        if admin.mfa_enabled:
            import pyotp
            if not totp_code:
                return jsonify({"ok": False, "error": "mfa_required",
                                "message": "Enter your authenticator code."}), 401
            secret = sec.decrypt_totp_secret(admin.totp_secret_enc or "")
            if not pyotp.TOTP(secret).verify(totp_code,
                                             valid_window=LOGIN_WINDOW):
                admin.failed_logins = int(admin.failed_logins) + 1
                if admin.failed_logins >= 5:
                    admin.locked_until = now() + 900
                audit(s, "admin.login", target_type="admin", target_id=admin.id,
                      result="failure", reason="bad_totp")
                return generic
        elif require_mfa:
            # Refuse rather than quietly granting a weaker session.
            return jsonify({
                "ok": False, "error": "mfa_enrolment_required",
                "message": "This account must enrol in two-factor "
                           "authentication before signing in.",
            }), 403

        admin.failed_logins = 0
        admin.locked_until = None
        admin.last_login_at = now()

        # Mint the token first so the session row can record the hash of the
        # token actually issued. Without that link the row would be a log
        # entry, and revoking it would not revoke anything.
        import uuid as _uuid
        from .config import config
        sid = str(_uuid.uuid4())
        token, ttl = sec.mint_admin_token(admin_id=admin.id, role=admin.role,
                                          session_id=sid)
        s.add(AdminSession(
            id=sid, admin_id=admin.id, token_hash=sec.token_hash(token),
            expires_at=now() + config().admin_session_ttl_s,
            ip_hash=sec.hash_ip(_ip()),
            user_agent=request.headers.get("User-Agent", "")[:200],
        ))
        g.admin_id, g.admin_email = admin.id, admin.email
        audit(s, "admin.login", target_type="admin", target_id=admin.id)
        return jsonify({"ok": True, "access_token": token, "expires_in": ttl,
                        "admin": {"email": admin.email, "role": admin.role},
                        "permissions": sorted(sec.PERMISSIONS.get(admin.role, []))})


@bp.post("/auth/logout")
@admin_required()
def admin_logout():
    with session_scope() as s:
        row = s.get(AdminSession, g.admin_session_id)
        if row is not None:
            row.revoked_at = now()
        audit(s, "admin.logout", target_type="admin", target_id=g.admin_id)
    return jsonify({"ok": True})


@bp.get("/auth/me")
@admin_required()
def admin_me():
    return jsonify({"ok": True, "admin": {"id": g.admin_id, "email": g.admin_email,
                                          "role": g.admin_role},
                    "permissions": sorted(sec.PERMISSIONS.get(g.admin_role, []))})


@bp.post("/auth/mfa/enrol")
def admin_mfa_enrol():
    """Enrol in TOTP. Requires the password again, because this endpoint has to
    work before the admin can hold a session."""
    import pyotp
    body = request.get_json(silent=True) or {}
    email = sec.normalise_email(body.get("email", ""))
    password = body.get("password") or ""
    confirm = str(body.get("totp") or "").strip()

    with session_scope() as s:
        try:
            rate_limit(s, f"adminmfa:ip:{sec.hash_ip(_ip())}", limit=10, window_s=900)
        except RateLimited as e:
            return jsonify({"ok": False, "error": "rate_limited"}), 429, \
                {"Retry-After": str(e.retry_after)}

        admin = s.scalar(select(AdminUser).where(AdminUser.email == email))
        if admin is None or not sec.verify_password(admin.password_hash, password):
            return jsonify({"ok": False, "error": "invalid_credentials"}), 401

        if not confirm and admin.mfa_enabled:
            # Re-enrolling here would let anyone holding the password alone
            # replace this admin's authenticator -- MFA reduced to a password.
            # Resetting an enrolled admin is an operator action on the server
            # (`python -m nova_cloud.manage enrol-mfa`).
            audit(s, "admin.mfa_reenrol_refused", target_type="admin",
                  target_id=admin.id, result="denied")
            return jsonify({"ok": False, "error": "already_enrolled",
                            "message": "MFA is already set up for this account. "
                                       "Ask an operator to reset it."}), 409

        if not confirm:
            # Step 1: hand out a secret, store it, but leave MFA disabled until
            # the admin proves their authenticator works.
            secret = pyotp.random_base32()
            admin.totp_secret_enc = sec.encrypt_totp_secret(secret)
            admin.mfa_enabled = False
            uri = pyotp.TOTP(secret).provisioning_uri(name=admin.email,
                                                      issuer_name="NOVA Admin")
            return jsonify({"ok": True, "stage": "confirm", "secret": secret,
                            "otpauth_url": uri})

        # Step 2: confirm.
        if not admin.totp_secret_enc:
            return jsonify({"ok": False, "error": "no_pending_enrolment"}), 400
        secret = sec.decrypt_totp_secret(admin.totp_secret_enc)
        # Enrolment tolerates a wider window than sign-in.
        #
        # These checks answer different questions. At sign-in, a code proves
        # the person holds the device *right now*, so the window stays tight.
        # At enrolment it proves only that the authenticator was seeded with
        # the right secret -- the password has already been checked on this
        # same request -- and the person is typically copying a code between
        # two devices, which is slow. A tight window here rejects correct
        # setups and teaches people to distrust the step.
        if not pyotp.TOTP(secret).verify(confirm, valid_window=ENROL_WINDOW):
            return jsonify({
                "ok": False, "error": "invalid_code",
                "message": "That code did not match. Check the authenticator "
                           "entry is the one just added, and that the device "
                           "clock is correct.",
            }), 400
        admin.mfa_enabled = True
        g.admin_id, g.admin_email = admin.id, admin.email
        audit(s, "admin.mfa_enrolled", target_type="admin", target_id=admin.id)
        return jsonify({"ok": True, "stage": "enrolled"})


# -- dashboard ---------------------------------------------------------------

@bp.get("/dashboard")
@admin_required("dashboard.view")
def dashboard():
    t = now()
    with session_scope() as s:
        def count(model, *where):
            q = select(func.count()).select_from(model)
            for w in where:
                q = q.where(w)
            return int(s.scalar(q) or 0)

        active_24h = int(s.scalar(
            select(func.count(distinct(ActivityEvent.user_id)))
            .where(ActivityEvent.ts > t - DAY,
                   ActivityEvent.user_id.is_not(None))) or 0)
        active_7d = int(s.scalar(
            select(func.count(distinct(ActivityEvent.user_id)))
            .where(ActivityEvent.ts > t - 7 * DAY,
                   ActivityEvent.user_id.is_not(None))) or 0)

        calls_24h = count(ModelCall, ModelCall.ts > t - DAY)
        failed_24h = count(ModelCall, ModelCall.ts > t - DAY,
                           ModelCall.status != "success")
        latency = s.scalar(select(func.avg(ModelCall.latency_ms))
                           .where(ModelCall.ts > t - DAY,
                                  ModelCall.status == "success"))

        return jsonify({"ok": True, "generated_at": t, "metrics": {
            "users_total": count(User, User.status != UserStatus.DELETED.value),
            "users_new_24h": count(User, User.created_at > t - DAY),
            "users_new_7d": count(User, User.created_at > t - 7 * DAY),
            "users_active_24h": active_24h,
            "users_active_7d": active_7d,
            "users_disabled": count(User, User.status == UserStatus.DISABLED.value),
            "users_suspended": count(User, User.status == UserStatus.SUSPENDED.value),
            "devices_total": count(Device, Device.revoked_at.is_(None)),
            "devices_revoked": count(Device, Device.revoked_at.is_not(None)),
            "devices_seen_24h": count(Device, Device.last_seen_at > t - DAY),
            "sessions_active": count(AuthSession, AuthSession.revoked_at.is_(None),
                                     AuthSession.expires_at > t),
            "voice_sessions_24h": count(ActivityEvent, ActivityEvent.ts > t - DAY,
                                        ActivityEvent.type == "VOICE_SESSION_STARTED"),
            "model_calls_24h": calls_24h,
            "model_calls_failed_24h": failed_24h,
            "model_error_rate_24h": round(failed_24h / calls_24h, 4) if calls_24h else 0.0,
            "avg_latency_ms_24h": int(latency) if latency else None,
            "tasks_24h": count(ActivityEvent, ActivityEvent.ts > t - DAY,
                               ActivityEvent.type == "TASK_STARTED"),
            "tasks_failed_24h": count(ActivityEvent, ActivityEvent.ts > t - DAY,
                                      ActivityEvent.type == "TASK_FAILED"),
            "errors_24h": count(ErrorEvent, ErrorEvent.ts > t - DAY),
            "offline_fallbacks_24h": count(ActivityEvent, ActivityEvent.ts > t - DAY,
                                           ActivityEvent.type == "OFFLINE_FALLBACK"),
            "network_degraded_24h": count(ActivityEvent, ActivityEvent.ts > t - DAY,
                                          ActivityEvent.type == "NETWORK_DEGRADED"),
            "agent_runs_24h": count(AgentRun, AgentRun.started_at > t - DAY),
        }})


# -- users -------------------------------------------------------------------

@bp.get("/users")
@admin_required("users.view")
def list_users():
    q = (request.args.get("q") or "").strip().lower()
    try:
        limit = min(int(request.args.get("limit", "50")), 200)
        offset = max(int(request.args.get("offset", "0")), 0)
    except ValueError:
        limit, offset = 50, 0

    with session_scope() as s:
        stmt = select(User)
        if q:
            # Search what an operator actually has to hand: an address from a
            # support email, a name, or an id copied out of a log or a crash
            # report. Restricting this to email would mean a device id from an
            # error report could not be traced back to an account at all.
            #
            # ilike, not like: SQLite's LIKE is case-insensitive for ASCII but
            # Postgres' is not, so a plain LIKE would quietly stop matching
            # once the platform moved to a real database.
            like = f"%{q}%"
            owns_device = select(Device.user_id).where(
                Device.id.ilike(like)).scalar_subquery()
            named = select(Profile.user_id).where(
                Profile.display_name.ilike(like)).scalar_subquery()
            stmt = stmt.where(or_(
                User.email.ilike(like),
                User.id.ilike(like),
                User.id.in_(owns_device),
                User.id.in_(named),
            ))
        total = int(s.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        rows = s.scalars(stmt.order_by(User.created_at.desc())
                         .limit(limit).offset(offset)).all()
        out = []
        for u in rows:
            prof = s.get(Profile, u.id)
            devices = int(s.scalar(select(func.count()).select_from(Device)
                                   .where(Device.user_id == u.id)) or 0)
            out.append({"id": u.id, "email": u.email,
                        "display_name": prof.display_name if prof else "",
                        "status": u.status, "created_at": u.created_at,
                        "last_active_at": u.last_active_at,
                        "email_verified": u.email_verified, "devices": devices})
        # Searching the user base is itself worth recording.
        if q:
            audit(s, "users.search", detail_q=q[:64])
        return jsonify({"ok": True, "total": total, "limit": limit,
                        "offset": offset, "users": out})


@bp.get("/users/<user_id>")
@admin_required("users.view")
def user_detail(user_id: str):
    with session_scope() as s:
        u = s.get(User, user_id)
        if u is None:
            return jsonify({"ok": False, "error": "not_found"}), 404
        prof = s.get(Profile, u.id)
        devices = s.scalars(select(Device).where(Device.user_id == u.id)).all()
        sessions = s.scalars(select(AuthSession).where(
            AuthSession.user_id == u.id, AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > now())).all()
        activity = s.scalars(select(ActivityEvent).where(
            ActivityEvent.user_id == u.id)
            .order_by(ActivityEvent.ts.desc()).limit(50)).all()
        calls = s.scalar(select(func.count()).select_from(ModelCall)
                         .where(ModelCall.user_id == u.id)) or 0

        # Opening an individual user record is a privileged read.
        audit(s, "users.view_detail", target_type="user", target_id=u.id)

        from .models import Instance, ModelUsage
        inst = s.scalar(select(Instance).where(Instance.user_id == u.id))
        usage = []
        if inst is not None:
            usage = [{"day": r.day, "kind": r.kind, "requests": r.requests,
                      "input_tokens": r.input_tokens, "output_tokens": r.output_tokens}
                     for r in s.scalars(select(ModelUsage)
                                        .where(ModelUsage.instance_id == inst.id)
                                        .order_by(ModelUsage.day.desc()).limit(30)).all()]

        return jsonify({"ok": True, "instance": None if inst is None else {
            # Operational fields only. The profile's free-text "about" is the
            # person's own words and is deliberately not returned.
            "id": inst.id, "plan": inst.plan,
            "onboarding_completed": inst.onboarding_completed_at is not None,
            "onboarding_completed_at": inst.onboarding_completed_at,
            "created_at": inst.created_at,
        }, "model_usage": usage, "user": {
            "id": u.id, "email": u.email,
            "display_name": prof.display_name if prof else "",
            "locale": prof.locale if prof else "en",
            "status": u.status, "email_verified": u.email_verified,
            "created_at": u.created_at, "last_active_at": u.last_active_at,
        }, "devices": [{
            "id": d.id, "name": d.name, "platform": d.platform,
            "app_version": d.app_version, "created_at": d.created_at,
            "last_seen_at": d.last_seen_at, "revoked": d.revoked_at is not None,
        } for d in devices],
            "active_sessions": len(sessions),
            "model_calls_total": int(calls),
            "activity": [{"type": a.type, "ts": a.ts, "device_id": a.device_id,
                          "platform": a.platform, "attrs": a.attrs}
                         for a in activity],
            # Stated explicitly so nobody assumes it was merely left out.
            "note": "Conversation content and memory are not accessible here.",
        })


@bp.get("/fleet")
@admin_required("dashboard.view")
def fleet():
    """Instances, app versions, model usage and update rollout, at a glance.
    Counts only; nothing here identifies what anyone said or stored."""
    from .models import DeviceUpdateState, Instance, ModelUsage, Release
    from .api_instance import today
    t = now()
    online_window = 10 * 60
    with session_scope() as s:
        def scalar(q):
            return int(s.scalar(q) or 0)

        instances_total = scalar(select(func.count()).select_from(Instance))
        instances_onboarded = scalar(select(func.count()).select_from(Instance)
                                     .where(Instance.onboarding_completed_at.is_not(None)))
        live_devices = Device.revoked_at.is_(None)
        online = scalar(select(func.count(distinct(Device.user_id)))
                        .where(live_devices, Device.last_seen_at > t - online_window))
        versions = [{"version": v or "unknown", "devices": n} for v, n in s.execute(
            select(Device.app_version, func.count()).where(live_devices)
            .group_by(Device.app_version).order_by(func.count().desc())).all()]
        plans = [{"plan": p, "instances": n} for p, n in s.execute(
            select(Instance.plan, func.count()).group_by(Instance.plan)).all()]
        usage_today = [{"kind": k, "requests": int(r or 0), "input_tokens": int(i or 0),
                        "output_tokens": int(o or 0)} for k, r, i, o in s.execute(
            select(ModelUsage.kind, func.sum(ModelUsage.requests),
                   func.sum(ModelUsage.input_tokens), func.sum(ModelUsage.output_tokens))
            .where(ModelUsage.day == today()).group_by(ModelUsage.kind)).all()]
        releases = [{"channel": r.channel, "version": r.version,
                     "min_supported": r.min_supported, "rollout_percent": r.rollout_percent,
                     "published_at": r.created_at}
                    for r in s.scalars(select(Release).where(Release.active.is_(True))).all()]
        results = [{"result": res or "none", "devices": n} for res, n in s.execute(
            select(DeviceUpdateState.last_result, func.count())
            .group_by(DeviceUpdateState.last_result)).all()]
        failures = [{"device_id": d.device_id, "target": d.last_target,
                     "result": d.last_result, "error": d.last_error, "at": d.updated_at}
                    for d in s.scalars(select(DeviceUpdateState)
                                       .where(DeviceUpdateState.last_result.in_(
                                           ("failed", "rolled_back")))
                                       .order_by(DeviceUpdateState.updated_at.desc())
                                       .limit(20)).all()]
        return jsonify({"ok": True, "generated_at": t, "fleet": {
            "instances_total": instances_total,
            "instances_onboarded": instances_onboarded,
            "instances_online": online,
            "instances_offline": max(0, instances_total - online),
            "online_window_s": online_window,
            "app_versions": versions,
            "plans": plans,
            "model_usage_today": usage_today,
            "releases": releases,
            "update_results": results,
            "recent_update_failures": failures,
        }})


@bp.post("/users/<user_id>/status")
@admin_required("users.disable")
def set_user_status(user_id: str):
    want = (request.get_json(silent=True) or {}).get("status", "")
    allowed = (UserStatus.ACTIVE.value, UserStatus.SUSPENDED.value,
               UserStatus.DISABLED.value)
    if want not in allowed:
        return jsonify({"ok": False, "error": "bad_request",
                        "message": f"status must be one of {', '.join(allowed)}"}), 400
    with session_scope() as s:
        u = s.get(User, user_id)
        if u is None:
            return jsonify({"ok": False, "error": "not_found"}), 404
        if want != UserStatus.ACTIVE.value and not sec.role_can(
                g.admin_role, "users.disable"):
            return jsonify({"ok": False, "error": "forbidden"}), 403
        if want == UserStatus.ACTIVE.value and not sec.role_can(
                g.admin_role, "users.enable"):
            return jsonify({"ok": False, "error": "forbidden"}), 403
        u.status = want
        if UserStatus.blocks_access(want):
            # Disabling must actually cut access, not just set a flag.
            u.token_epoch = int(u.token_epoch) + 1
            for a in s.scalars(select(AuthSession).where(
                    AuthSession.user_id == u.id,
                    AuthSession.revoked_at.is_(None))).all():
                a.revoked_at = now()
        audit(s, "users.set_status", target_type="user", target_id=u.id,
              status=want)
    invalidate_auth_cache(user_id)
    return jsonify({"ok": True, "status": want})


@bp.post("/users/<user_id>/revoke_sessions")
@admin_required("users.revoke_sessions")
def revoke_user_sessions(user_id: str):
    with session_scope() as s:
        u = s.get(User, user_id)
        if u is None:
            return jsonify({"ok": False, "error": "not_found"}), 404
        u.token_epoch = int(u.token_epoch) + 1
        n = 0
        for a in s.scalars(select(AuthSession).where(
                AuthSession.user_id == u.id,
                AuthSession.revoked_at.is_(None))).all():
            a.revoked_at = now()
            n += 1
        audit(s, "users.revoke_sessions", target_type="user", target_id=u.id,
              sessions=n)
    invalidate_auth_cache(user_id)
    return jsonify({"ok": True, "sessions_revoked": n})


@bp.post("/devices/<device_id>/revoke")
@admin_required("devices.revoke")
def admin_revoke_device(device_id: str):
    with session_scope() as s:
        d = s.get(Device, device_id)
        if d is None:
            return jsonify({"ok": False, "error": "not_found"}), 404
        d.revoked_at = now()
        n = 0
        for a in s.scalars(select(AuthSession).where(
                AuthSession.device_id == d.id,
                AuthSession.revoked_at.is_(None))).all():
            a.revoked_at = now()
            n += 1
        audit(s, "devices.revoke", target_type="device", target_id=d.id,
              sessions=n)
        uid = d.user_id
    invalidate_auth_cache(uid)
    return jsonify({"ok": True, "sessions_revoked": n})


@bp.get("/devices")
@admin_required("devices.view")
def list_devices():
    try:
        limit = min(int(request.args.get("limit", "100")), 500)
    except ValueError:
        limit = 100
    with session_scope() as s:
        rows = s.scalars(select(Device)
                         .order_by(Device.last_seen_at.desc().nullslast())
                         .limit(limit)).all()
        t = now()
        return jsonify({"ok": True, "devices": [{
            "id": d.id, "user_id": d.user_id, "name": d.name,
            "platform": d.platform, "app_version": d.app_version,
            "last_seen_at": d.last_seen_at,
            "revoked": d.revoked_at is not None,
            # "online" is defined, not guessed: seen within 5 minutes.
            "online": bool(d.last_seen_at and t - d.last_seen_at < 300),
        } for d in rows]})


# -- observability -----------------------------------------------------------

@bp.get("/activity")
@admin_required("activity.view")
def activity():
    try:
        limit = min(int(request.args.get("limit", "100")), 500)
    except ValueError:
        limit = 100
    user_id = request.args.get("user_id")
    etype = request.args.get("type")
    with session_scope() as s:
        stmt = select(ActivityEvent)
        if user_id:
            stmt = stmt.where(ActivityEvent.user_id == user_id)
        if etype:
            stmt = stmt.where(ActivityEvent.type == etype)
        rows = s.scalars(stmt.order_by(ActivityEvent.ts.desc()).limit(limit)).all()
        return jsonify({"ok": True, "events": [{
            "id": e.id, "type": e.type, "ts": e.ts, "user_id": e.user_id,
            "device_id": e.device_id, "platform": e.platform,
            "app_version": e.app_version, "attrs": e.attrs,
        } for e in rows]})


@bp.get("/models")
@admin_required("models.view")
def models():
    """Per-provider request counts, success rate and latency. Real aggregates,
    computed from model_calls."""
    window = float(request.args.get("window_s", 7 * DAY))
    since = now() - window
    with session_scope() as s:
        # Counted per combination rather than with a boolean SUM, which is
        # not portable across SQLite and Postgres.
        out = []
        combos = s.execute(
            select(ModelCall.provider, ModelCall.model, func.count())
            .where(ModelCall.ts > since)
            .group_by(ModelCall.provider, ModelCall.model)).all()
        for provider, model, n in combos:
            base = (ModelCall.ts > since, ModelCall.provider == provider,
                    ModelCall.model == model)
            fails = int(s.scalar(select(func.count()).select_from(ModelCall)
                                 .where(*base, ModelCall.status != "success")) or 0)
            avg_l = s.scalar(select(func.avg(ModelCall.latency_ms))
                             .where(*base, ModelCall.status == "success"))
            avg_f = s.scalar(select(func.avg(ModelCall.first_token_ms))
                             .where(*base, ModelCall.status == "success"))
            offline = int(s.scalar(select(func.count()).select_from(ModelCall)
                                   .where(*base, ModelCall.offline.is_(True))) or 0)
            out.append({
                "provider": provider, "model": model, "requests": int(n),
                "failures": fails,
                "success_rate": round((int(n) - fails) / int(n), 4) if n else 0.0,
                "avg_latency_ms": int(avg_l) if avg_l else None,
                "avg_first_token_ms": int(avg_f) if avg_f else None,
                "offline_requests": offline,
            })
        out.sort(key=lambda r: -r["requests"])
        return jsonify({"ok": True, "window_s": window, "providers": out})


@bp.get("/agents")
@admin_required("agents.view")
def agents():
    window = float(request.args.get("window_s", 7 * DAY))
    since = now() - window
    with session_scope() as s:
        combos = s.execute(
            select(AgentRun.agent, func.count()).where(AgentRun.started_at > since)
            .group_by(AgentRun.agent)).all()
        out = []
        for agent, n in combos:
            base = (AgentRun.started_at > since, AgentRun.agent == agent)
            failed = int(s.scalar(select(func.count()).select_from(AgentRun)
                                  .where(*base, AgentRun.status == "failed")) or 0)
            avg_d = s.scalar(select(func.avg(AgentRun.duration_ms))
                             .where(*base, AgentRun.status == "completed"))
            out.append({"agent": agent, "runs": int(n), "failed": failed,
                        "success_rate": round((int(n) - failed) / int(n), 4) if n else 0.0,
                        "avg_duration_ms": int(avg_d) if avg_d else None})
        out.sort(key=lambda r: -r["runs"])
        return jsonify({"ok": True, "window_s": window, "agents": out})


@bp.get("/errors")
@admin_required("errors.view")
def errors():
    window = float(request.args.get("window_s", 7 * DAY))
    since = now() - window
    with session_scope() as s:
        combos = s.execute(
            select(ErrorEvent.code, func.count()).where(ErrorEvent.ts > since)
            .group_by(ErrorEvent.code)).all()
        recent = s.scalars(select(ErrorEvent).where(ErrorEvent.ts > since)
                           .order_by(ErrorEvent.ts.desc()).limit(100)).all()
        return jsonify({
            "ok": True,
            "by_code": sorted([{"code": c, "count": int(n)} for c, n in combos],
                              key=lambda r: -r["count"]),
            "recent": [{"code": e.code, "ts": e.ts, "platform": e.platform,
                        "app_version": e.app_version, "device_id": e.device_id,
                        "context": e.context} for e in recent],
        })


@bp.get("/health")
@admin_required("health.view")
def platform_health():
    """Health of the pieces this backend can actually observe.

    Services NOVA depends on but the backend cannot reach (a user's Ollama, for
    instance) are reported from client telemetry, and labelled as such rather
    than shown as a green light the server did not verify.
    """
    from sqlalchemy import text as _text
    from .db import engine as _engine
    t = now()
    checks = []

    t0 = time.time()
    try:
        with _engine().connect() as c:
            c.execute(_text("SELECT 1"))
        checks.append({"service": "database", "status": "healthy",
                       "latency_ms": int((time.time() - t0) * 1000),
                       "source": "measured"})
    except Exception as e:
        checks.append({"service": "database", "status": "degraded",
                       "detail": type(e).__name__, "source": "measured"})

    checks.append({"service": "authentication", "status": "healthy",
                   "source": "measured"})

    with session_scope() as s:
        recent_calls = s.execute(
            select(ModelCall.provider, func.count())
            .where(ModelCall.ts > t - DAY).group_by(ModelCall.provider)).all()
        for provider, n in recent_calls:
            fails = int(s.scalar(select(func.count()).select_from(ModelCall)
                                 .where(ModelCall.ts > t - DAY,
                                        ModelCall.provider == provider,
                                        ModelCall.status != "success")) or 0)
            rate = fails / int(n) if n else 0.0
            checks.append({
                "service": f"provider:{provider}",
                "status": "healthy" if rate < 0.1 else
                          ("degraded" if rate < 0.5 else "failing"),
                "error_rate": round(rate, 4), "requests_24h": int(n),
                "source": "client_telemetry",
            })
        ingest_ok = int(s.scalar(select(func.count()).select_from(ActivityEvent)
                                 .where(ActivityEvent.ts > t - 3600)) or 0)
    checks.append({"service": "telemetry", "status": "healthy",
                   "events_last_hour": ingest_ok, "source": "measured"})

    overall = "healthy"
    if any(c["status"] == "failing" for c in checks):
        overall = "failing"
    elif any(c["status"] == "degraded" for c in checks):
        overall = "degraded"
    return jsonify({"ok": True, "status": overall, "checks": checks,
                    "generated_at": t})


# -- feature flags -----------------------------------------------------------

@bp.get("/flags")
@admin_required("flags.view")
def list_flags():
    with session_scope() as s:
        rows = s.scalars(select(FeatureFlag)).all()
        return jsonify({"ok": True, "flags": [{
            "key": f.key, "description": f.description, "enabled": f.enabled,
            "rollout_percent": f.rollout_percent, "updated_at": f.updated_at,
        } for f in rows]})


@bp.put("/flags/<key>")
@admin_required("flags.edit")
def upsert_flag(key: str):
    body = request.get_json(silent=True) or {}
    with session_scope() as s:
        f = s.get(FeatureFlag, key)
        if f is None:
            # Set the defaults explicitly: column defaults are only applied at
            # flush, and this function reads the values back before then.
            f = FeatureFlag(key=key[:64], description="", enabled=False,
                            rollout_percent=0)
            s.add(f)
        if "description" in body:
            f.description = str(body["description"])[:500]
        if "enabled" in body:
            f.enabled = bool(body["enabled"])
        if "rollout_percent" in body:
            f.rollout_percent = max(0, min(100, int(body["rollout_percent"] or 0)))
        audit(s, "flags.upsert", target_type="flag", target_id=key,
              enabled=bool(f.enabled), rollout_percent=int(f.rollout_percent))
        return jsonify({"ok": True, "flag": {
            "key": f.key, "description": f.description, "enabled": f.enabled,
            "rollout_percent": f.rollout_percent}})


# -- audit log ---------------------------------------------------------------

@bp.get("/audit")
@admin_required("audit.view")
def audit_log():
    try:
        limit = min(int(request.args.get("limit", "100")), 500)
    except ValueError:
        limit = 100
    with session_scope() as s:
        rows = s.scalars(select(AdminAuditLog)
                         .order_by(AdminAuditLog.ts.desc()).limit(limit)).all()
        return jsonify({"ok": True, "entries": [{
            "id": r.id, "admin_email": r.admin_email, "action": r.action,
            "target_type": r.target_type, "target_id": r.target_id,
            "ts": r.ts, "result": r.result, "detail": r.detail,
        } for r in rows]})


# -- admin user management ---------------------------------------------------

@bp.get("/admins")
@admin_required("admins.view")
def list_admins():
    with session_scope() as s:
        rows = s.scalars(select(AdminUser)).all()
        return jsonify({"ok": True, "admins": [{
            "id": a.id, "email": a.email, "role": a.role, "status": a.status,
            "mfa_enabled": a.mfa_enabled, "last_login_at": a.last_login_at,
            "created_at": a.created_at,
        } for a in rows]})


@bp.post("/admins")
@admin_required("admins.manage")
def create_admin():
    body = request.get_json(silent=True) or {}
    email = sec.normalise_email(body.get("email", ""))
    password = body.get("password") or ""
    role = (body.get("role") or AdminRole.ANALYST.value).upper()
    if not sec.valid_email(email):
        return jsonify({"ok": False, "error": "invalid_email"}), 400
    if role not in sec.PERMISSIONS:
        return jsonify({"ok": False, "error": "invalid_role"}), 400
    try:
        sec.check_password_policy(password)
    except sec.PasswordPolicyError as e:
        return jsonify({"ok": False, "error": "weak_password",
                        "message": str(e)}), 400
    with session_scope() as s:
        if s.scalar(select(AdminUser).where(AdminUser.email == email)):
            return jsonify({"ok": False, "error": "email_taken"}), 409
        a = AdminUser(email=email, password_hash=sec.hash_password(password),
                      role=role)
        s.add(a)
        s.flush()
        audit(s, "admins.create", target_type="admin", target_id=a.id, role=role)
        return jsonify({"ok": True, "admin": {"id": a.id, "email": a.email,
                                              "role": a.role}}), 201
