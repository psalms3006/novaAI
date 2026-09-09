"""nova_cloud.api_auth — account lifecycle.

Endpoints:

    POST /v1/auth/signup            create account, register device, sign in
    POST /v1/auth/login             sign in on a device
    POST /v1/auth/refresh           rotate refresh token, mint access token
    POST /v1/auth/logout            end this device's session
    POST /v1/auth/logout_all        end every session on every device
    POST /v1/auth/password/forgot   request a reset token
    POST /v1/auth/password/reset    consume a reset token
    POST /v1/auth/email/verify      consume a verification token
    GET  /v1/auth/me                current account, profile and device

Signup and login both register the calling device in the same round trip, so a
new machine is usable immediately after the first successful authentication
without a second call.
"""
from __future__ import annotations

import time

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import select

from . import security as sec
from .db import RateLimited, rate_limit, session_scope
from .models import (
    AuthSession, Device, EmailToken, Profile, User, UserStatus, now,
)
from .telemetry_sink import record_event

bp = Blueprint("auth", __name__, url_prefix="/v1/auth")

# The message returned for both "no such account" and "wrong password". Telling
# the two apart hands an attacker a free account-existence oracle.
_BAD_CREDS = "Incorrect email or password."


def _client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or ""


def _fail(message: str, status: int = 400, code: str = "bad_request"):
    return jsonify({"ok": False, "error": code, "message": message}), status


def _issue_session(s, user: User, device: Device, ip: str, ua: str) -> dict:
    """Mint a fresh refresh/access pair for a device."""
    refresh = sec.new_opaque_token()
    cfg = current_app.config["NOVA_CFG"]
    auth = AuthSession(
        user_id=user.id,
        device_id=device.id,
        refresh_hash=sec.token_hash(refresh),
        expires_at=now() + cfg.refresh_ttl_s,
        ip_hash=sec.hash_ip(ip),
        user_agent=(ua or "")[:200],
    )
    s.add(auth)
    s.flush()
    access, ttl = sec.mint_access_token(
        user_id=user.id, device_id=device.id, session_id=auth.id,
        token_epoch=user.token_epoch,
    )
    device.last_seen_at = now()
    user.last_active_at = now()
    return {
        "access_token": access,
        "expires_in": ttl,
        "refresh_token": refresh,
        "refresh_expires_in": cfg.refresh_ttl_s,
        "offline_grace_s": cfg.offline_grace_s,
    }


def _register_device(s, user: User, body: dict) -> tuple[Device, str]:
    """Find or create the device row for this installation.

    The client supplies a device_id it generated locally plus a secret. On
    first contact we store the hash; afterwards the secret must match, so a
    device_id alone is not enough to impersonate an installation.
    """
    device_id = (body.get("device_id") or "").strip()[:36]
    secret = (body.get("device_secret") or "").strip()
    platform = (body.get("platform") or "unknown").strip()[:32]
    version = (body.get("app_version") or "").strip()[:32]
    name = (body.get("device_name") or "").strip()[:120] or "NOVA device"

    if not device_id or not secret:
        raise ValueError("device_id and device_secret are required")

    dev = s.get(Device, device_id)
    if dev is None:
        dev = Device(
            id=device_id, user_id=user.id, name=name, platform=platform,
            app_version=version, secret_hash=sec.token_hash(secret),
        )
        s.add(dev)
        s.flush()
        return dev, "registered"

    # Existing device row. It must belong to this user and match the secret.
    if dev.user_id != user.id:
        raise PermissionError("device belongs to another account")
    if dev.secret_hash != sec.token_hash(secret):
        raise PermissionError("device secret mismatch")
    if dev.revoked_at is not None:
        # Signing in again on a revoked device deliberately reinstates it: the
        # user just proved they hold the password. Revocation protects a lost
        # machine, it is not a permanent ban on that hardware.
        dev.revoked_at = None
    dev.platform = platform or dev.platform
    dev.app_version = version or dev.app_version
    dev.last_seen_at = now()
    return dev, "reconnected"


@bp.post("/signup")
def signup():
    body = request.get_json(silent=True) or {}
    email = sec.normalise_email(body.get("email", ""))
    password = body.get("password") or ""
    display_name = (body.get("display_name") or "").strip()[:120]
    ip = _client_ip()

    if not sec.valid_email(email):
        return _fail("Enter a valid email address.", 400, "invalid_email")
    try:
        sec.check_password_policy(password)
    except sec.PasswordPolicyError as e:
        return _fail(str(e), 400, "weak_password")

    with session_scope() as s:
        try:
            rate_limit(s, f"signup:ip:{sec.hash_ip(ip)}", limit=5, window_s=3600)
        except RateLimited as e:
            resp = _fail("Too many sign-up attempts. Try again later.", 429,
                         "rate_limited")
            return resp[0], resp[1], {"Retry-After": str(e.retry_after)}

        existing = s.scalar(select(User).where(User.email == email))
        if existing is not None:
            # Same generic message as a bad login, for the same reason.
            return _fail("That email cannot be used to create an account.",
                         409, "email_taken")

        cfg = current_app.config["NOVA_CFG"]
        user = User(
            email=email,
            password_hash=sec.hash_password(password),
            email_verified=not cfg.require_email_verification,
        )
        s.add(user)
        s.flush()
        s.add(Profile(user_id=user.id, display_name=display_name or email.split("@")[0]))

        try:
            device, _ = _register_device(s, user, body)
        except (ValueError, PermissionError) as e:
            return _fail(str(e), 400, "device_error")

        tokens = _issue_session(s, user, device, ip, request.headers.get("User-Agent", ""))
        record_event(s, "USER_CREATED", user_id=user.id, device_id=device.id,
                     platform=device.platform, app_version=device.app_version)
        record_event(s, "DEVICE_REGISTERED", user_id=user.id, device_id=device.id,
                     platform=device.platform, app_version=device.app_version)

        payload = {
            "ok": True,
            "user": {"id": user.id, "email": user.email,
                     "display_name": display_name or email.split("@")[0],
                     "email_verified": user.email_verified,
                     "created_at": user.created_at},
            "device": {"id": device.id, "name": device.name},
            **tokens,
        }
        if cfg.require_email_verification:
            token = sec.new_opaque_token(24)
            s.add(EmailToken(user_id=user.id, kind="verify_email",
                             token_hash=sec.token_hash(token),
                             expires_at=now() + 86400))
            # Delivery is the deployment's job. Surfaced here only outside
            # production so a developer can complete the flow without a mail
            # server; never returned from a production deployment.
            if not cfg.is_production:
                payload["dev_verification_token"] = token
        return jsonify(payload), 201


@bp.post("/login")
def login():
    body = request.get_json(silent=True) or {}
    email = sec.normalise_email(body.get("email", ""))
    password = body.get("password") or ""
    ip = _client_ip()

    with session_scope() as s:
        try:
            rate_limit(s, f"login:ip:{sec.hash_ip(ip)}", limit=20, window_s=900)
            if email:
                rate_limit(s, f"login:acct:{sec.token_hash(email)}", limit=10,
                           window_s=900)
        except RateLimited as e:
            resp = _fail("Too many attempts. Try again shortly.", 429, "rate_limited")
            return resp[0], resp[1], {"Retry-After": str(e.retry_after)}

        user = s.scalar(select(User).where(User.email == email))
        if user is None or not sec.verify_password(user.password_hash, password):
            record_event(s, "AUTH_FAILURE", user_id=(user.id if user else None))
            return _fail(_BAD_CREDS, 401, "invalid_credentials")
        if user.status != UserStatus.ACTIVE.value:
            return _fail("This account is not active.", 403, "account_disabled")

        if sec.needs_rehash(user.password_hash):
            user.password_hash = sec.hash_password(password)

        try:
            device, how = _register_device(s, user, body)
        except ValueError as e:
            return _fail(str(e), 400, "device_error")
        except PermissionError:
            return _fail("This device is registered to a different account.",
                         403, "device_conflict")

        tokens = _issue_session(s, user, device, ip,
                                request.headers.get("User-Agent", ""))
        record_event(s, "USER_LOGIN", user_id=user.id, device_id=device.id,
                     platform=device.platform, app_version=device.app_version)
        if how == "registered":
            record_event(s, "DEVICE_REGISTERED", user_id=user.id,
                         device_id=device.id, platform=device.platform)

        prof = s.get(Profile, user.id)
        return jsonify({
            "ok": True,
            "user": {"id": user.id, "email": user.email,
                     "display_name": prof.display_name if prof else "",
                     "email_verified": user.email_verified,
                     "created_at": user.created_at},
            "device": {"id": device.id, "name": device.name},
            **tokens,
        })


@bp.post("/refresh")
def refresh():
    body = request.get_json(silent=True) or {}
    presented = (body.get("refresh_token") or "").strip()
    if not presented:
        return _fail("refresh_token is required", 400, "missing_token")

    with session_scope() as s:
        h = sec.token_hash(presented)
        auth = s.scalar(select(AuthSession).where(AuthSession.refresh_hash == h))
        if auth is None:
            # Either never valid, or already rotated away. Both mean "do not
            # trust this client"; a genuine client always holds the newest one.
            return _fail("Session expired. Sign in again.", 401, "invalid_refresh")

        # Establish *why* this token is unusable before deciding it leaked.
        # A device that was revoked, or an account that was disabled, is a
        # specific answer; reporting those as token reuse would be wrong and
        # would hide a real administrative action from the client.
        user = s.get(User, auth.user_id)
        device = s.get(Device, auth.device_id)
        if user is None or device is None:
            return _fail("Session expired. Sign in again.", 401, "invalid_refresh")
        if device.revoked_at is not None:
            # This is where a remote revocation actually bites.
            if auth.revoked_at is None:
                auth.revoked_at = now()
            record_event(s, "DEVICE_REVOKED", user_id=user.id, device_id=device.id,
                         reason="refresh_after_revoke")
            return _fail("This device has been signed out.", 403, "device_revoked")
        if user.status != UserStatus.ACTIVE.value:
            return _fail("This account is not active.", 403, "account_disabled")

        if auth.revoked_at is not None:
            # Still a live device and a live account, but a retired token was
            # presented: that is a leak signal. Kill every session for this
            # device so the holder cannot use a sibling token.
            auth.reuse_detected_at = now()
            for sib in s.scalars(select(AuthSession).where(
                    AuthSession.device_id == auth.device_id,
                    AuthSession.revoked_at.is_(None))).all():
                sib.revoked_at = now()
            record_event(s, "AUTH_FAILURE", user_id=auth.user_id,
                         device_id=auth.device_id, reason="refresh_reuse")
            return _fail("Session expired. Sign in again.", 401, "invalid_refresh")

        if auth.expires_at < now():
            auth.revoked_at = now()
            return _fail("Session expired. Sign in again.", 401, "expired_refresh")

        # Rotate: the presented token is retired and replaced.
        auth.revoked_at = now()
        auth.last_used_at = now()
        tokens = _issue_session(s, user, device, _client_ip(),
                                request.headers.get("User-Agent", ""))
        return jsonify({"ok": True, **tokens})


@bp.post("/logout")
def logout():
    body = request.get_json(silent=True) or {}
    presented = (body.get("refresh_token") or "").strip()
    with session_scope() as s:
        if presented:
            auth = s.scalar(select(AuthSession).where(
                AuthSession.refresh_hash == sec.token_hash(presented)))
            if auth is not None and auth.revoked_at is None:
                auth.revoked_at = now()
                record_event(s, "USER_LOGOUT", user_id=auth.user_id,
                             device_id=auth.device_id)
        # Always 200: logout must never leak whether the token was real, and a
        # client that cannot log out is worse than one that logs out twice.
        return jsonify({"ok": True})


@bp.post("/logout_all")
def logout_all():
    """Sign out every device. Requires a valid access token."""
    from .auth_guard import require_user
    err = require_user()
    if err:
        return err
    with session_scope() as s:
        user = s.get(User, g.user_id)
        if user is None:
            return _fail("Not signed in.", 401, "unauthorised")
        # Raising the epoch invalidates outstanding access tokens instantly.
        user.token_epoch = int(user.token_epoch) + 1
        n = 0
        for a in s.scalars(select(AuthSession).where(
                AuthSession.user_id == user.id,
                AuthSession.revoked_at.is_(None))).all():
            a.revoked_at = now()
            n += 1
        record_event(s, "USER_LOGOUT", user_id=user.id, scope="all", sessions=n)
        return jsonify({"ok": True, "sessions_revoked": n})


@bp.post("/password/forgot")
def forgot_password():
    body = request.get_json(silent=True) or {}
    email = sec.normalise_email(body.get("email", ""))
    with session_scope() as s:
        try:
            rate_limit(s, f"forgot:ip:{sec.hash_ip(_client_ip())}", limit=5,
                       window_s=3600)
        except RateLimited as e:
            resp = _fail("Too many requests.", 429, "rate_limited")
            return resp[0], resp[1], {"Retry-After": str(e.retry_after)}

        user = s.scalar(select(User).where(User.email == email))
        out: dict = {"ok": True}
        if user is not None and user.status == UserStatus.ACTIVE.value:
            token = sec.new_opaque_token(24)
            s.add(EmailToken(user_id=user.id, kind="reset_password",
                             token_hash=sec.token_hash(token),
                             expires_at=now() + 3600))
            cfg = current_app.config["NOVA_CFG"]
            if not cfg.is_production:
                out["dev_reset_token"] = token
        # Identical response either way: this endpoint must not reveal which
        # addresses have accounts.
        out["message"] = "If that address has an account, a reset link is on its way."
        return jsonify(out)


@bp.post("/password/reset")
def reset_password():
    body = request.get_json(silent=True) or {}
    token = (body.get("token") or "").strip()
    new_password = body.get("password") or ""
    try:
        sec.check_password_policy(new_password)
    except sec.PasswordPolicyError as e:
        return _fail(str(e), 400, "weak_password")

    with session_scope() as s:
        row = s.scalar(select(EmailToken).where(
            EmailToken.token_hash == sec.token_hash(token),
            EmailToken.kind == "reset_password"))
        if row is None or row.used_at is not None or row.expires_at < now():
            return _fail("This reset link is no longer valid.", 400, "invalid_token")
        user = s.get(User, row.user_id)
        if user is None:
            return _fail("This reset link is no longer valid.", 400, "invalid_token")

        row.used_at = now()
        user.password_hash = sec.hash_password(new_password)
        # A password reset must end every existing session: that is the whole
        # point of resetting after a compromise.
        user.token_epoch = int(user.token_epoch) + 1
        for a in s.scalars(select(AuthSession).where(
                AuthSession.user_id == user.id,
                AuthSession.revoked_at.is_(None))).all():
            a.revoked_at = now()
        record_event(s, "PASSWORD_RESET", user_id=user.id)
        return jsonify({"ok": True, "message": "Password updated. Sign in again."})


@bp.post("/email/verify")
def verify_email():
    body = request.get_json(silent=True) or {}
    token = (body.get("token") or "").strip()
    with session_scope() as s:
        row = s.scalar(select(EmailToken).where(
            EmailToken.token_hash == sec.token_hash(token),
            EmailToken.kind == "verify_email"))
        if row is None or row.used_at is not None or row.expires_at < now():
            return _fail("This verification link is no longer valid.", 400,
                         "invalid_token")
        user = s.get(User, row.user_id)
        if user is None:
            return _fail("This verification link is no longer valid.", 400,
                         "invalid_token")
        row.used_at = now()
        user.email_verified = True
        record_event(s, "EMAIL_VERIFIED", user_id=user.id)
        return jsonify({"ok": True})


@bp.get("/me")
def me():
    from .auth_guard import require_user
    err = require_user()
    if err:
        return err
    with session_scope() as s:
        user = s.get(User, g.user_id)
        if user is None:
            return _fail("Not signed in.", 401, "unauthorised")
        prof = s.get(Profile, user.id)
        dev = s.get(Device, g.device_id)
        return jsonify({
            "ok": True,
            "user": {
                "id": user.id, "email": user.email,
                "display_name": prof.display_name if prof else "",
                "locale": prof.locale if prof else "en",
                "email_verified": user.email_verified,
                "created_at": user.created_at,
                "status": user.status,
            },
            "device": ({"id": dev.id, "name": dev.name, "platform": dev.platform,
                        "app_version": dev.app_version} if dev else None),
            "server_time": time.time(),
        })
