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

import base64
import hashlib
import hmac as _hmac
import logging
import secrets
import time
from urllib.parse import quote, urlencode, urlsplit

from flask import Blueprint, current_app, g, jsonify, redirect, request
from sqlalchemy import select

import requests

from . import security as sec
from .db import RateLimited, rate_limit, session_scope
from . import mailer as mail
from .models import (
    AuthSession, Device, EmailToken, Profile, User, UserStatus, now,
)
from .telemetry_sink import record_event

bp = Blueprint("auth", __name__, url_prefix="/v1/auth")

log = logging.getLogger(__name__)

# The message returned for both "no such account" and "wrong password". Telling
# the two apart hands an attacker a free account-existence oracle.
_BAD_CREDS = "Incorrect email or password."


def _client_ip() -> str:
    from .auth_guard import client_ip
    return client_ip()


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


def _issue_email_token(s, user_id: str, kind: str, ttl_s: int) -> str:
    """Mint a single-use email token and store only its hash."""
    token = sec.new_opaque_token(24)
    s.add(EmailToken(user_id=user_id, kind=kind,
                     token_hash=sec.token_hash(token),
                     expires_at=now() + ttl_s))
    return token


def _delivery_state(mc) -> dict:
    """What the server can honestly say about email delivery.

    Deliberately independent of whether any particular account exists, so it
    can be returned on every response without turning the reset endpoint into
    an account-existence oracle.
    """
    return {"email_configured": bool(mc.configured),
            "email_provider": mc.cfg.provider if mc.configured else "none"}


def _sent_state(mc) -> dict:
    """Delivery state including whether *this* message went out.

    Only safe on endpoints where the caller already knows the account exists.
    Never use it on password reset: there, a `sent` flag that is true for real
    addresses and false for unknown ones is an account-existence oracle.
    """
    return {**_delivery_state(mc), "sent": False}


#: Account mail is awaited inside the request, so it gets a shorter leash than
#: the background sender. Long enough for a healthy provider, short enough that
#: sign-up does not hang on a degraded one.
_AUTH_EMAIL_TIMEOUT_S = 6.0


def _deliver(mc, address: str, template, token: str, kind: str,
             wrap_key: str | None = "email_delivery") -> dict:
    """Send one account email and report truthfully what happened.

    Delivery is synchronous and bounded, because the point of the return value
    is to tell a human whether a message was actually accepted. A queued send
    cannot answer that: it returns before the provider has ruled on the
    message, so `sent` would mean "handed to the queue", and the UI would say
    "check your inbox" for a message that was silently refused.

    There is deliberately no re-enqueue after a failure. A timeout can expire
    *after* the provider accepted the message, so a hidden retry risks a
    duplicate email; the caller is told the truth and the resend endpoint is
    one click away.

    When nothing is configured NOVA does not pretend: no "check your inbox",
    and outside production the token is handed back so a developer can finish
    the flow. That fallback is refused in production even if someone sets
    NOVA_ENV wrongly, because it is gated on the provider being absent *and*
    the environment not being production.
    """
    cfg = current_app.config["NOVA_CFG"]
    state = _delivery_state(mc)
    if mc.configured:
        try:
            subject, text, html_body = template(mc.cfg, token)
            mc.send_now(address, subject, text, html_body,
                        timeout=_AUTH_EMAIL_TIMEOUT_S)
            state["sent"] = True
        except mail.EmailRejected as e:
            # The provider understood the message and refused it. Retrying the
            # identical message cannot help; say so instead of implying success.
            state["sent"] = False
            state["error"] = "delivery_rejected"
            if e.status:
                state["provider_status"] = int(e.status)
            log.warning("[AUTH] %s email rejected by provider (status %s)",
                        kind, e.status or "4xx")
        except mail.EmailNotConfigured:
            state["sent"] = False
            state["error"] = "not_configured"
        except Exception as e:
            state["sent"] = False
            state["error"] = "delivery_failed"
            # Type only: the message can quote the recipient address, and the
            # address is what we are trying not to log.
            log.warning("[AUTH] %s email failed to send: %s",
                        kind, type(e).__name__)
    else:
        state["sent"] = False
        if not cfg.is_production:
            state["dev_token"] = token
    return {wrap_key: state} if wrap_key else state


def _notify_new_device(address: str, device) -> None:
    """Security notice for a first sign-in on an unfamiliar device."""
    mc = mail.mailer()
    if not mc.configured:
        return
    try:
        when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
        subject, text, html_body = mail.new_device_email(
            mc.cfg, device.name, device.platform, when)
        mc.send(address, subject, text, html_body, kind="new_device")
    except Exception:
        # A security notice failing must never fail the sign-in itself.
        pass


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

        # Check the device before creating anything. A failure after the user
        # row is added would be committed by session_scope on return, leaving
        # an account nobody can sign up for again ("email taken").
        new_device_id = (body.get("device_id") or "").strip()[:36]
        if not new_device_id or not (body.get("device_secret") or "").strip():
            return _fail("device_id and device_secret are required", 400, "device_error")
        if s.get(Device, new_device_id) is not None:
            return _fail("This device is registered to another account.", 400,
                         "device_error")

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
            s.rollback()
            return _fail(str(e), 400, "device_error")

        # Every account has its NOVA instance from the start (and older
        # accounts get theirs at next sign-in), so it is never missing
        # from anything that counts or looks for instances.
        from .api_instance import get_or_create_instance
        get_or_create_instance(s, user.id)
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
        # Verification is offered whenever email is configured, not only when
        # it is mandatory: an unverified account should still be able to prove
        # its address.
        mc = mail.mailer()
        if cfg.require_email_verification or mc.configured:
            token = _issue_email_token(s, user.id, "verify_email", 86400)
            payload.update(_deliver(mc, user.email, mail.verification_email,
                                    token, "verify_email"))
            # Also send a 6-digit verification code
            code = sec.new_verification_code()
            s.add(EmailToken(
                user_id=user.id,
                kind="verify_email",
                # token_hash is NOT NULL; a code-only row carries a
                # derived placeholder that cannot collide with a link
                # token (opaque tokens are urlsafe base64: no colon).
                token_hash="code:" + sec.hash_code(code),
                code_hash=sec.hash_code(code),
                expires_at=now() + 86400,
                code_expires_at=now() + 600,  # 10 minutes for code
            ))
            # Separate key: `email_delivery` must keep describing the link
            # email, since that is the message the UI says was sent.
            payload.update(_deliver(mc, user.email, mail.verification_code_email,
                                    code, "verify_email_code",
                                    wrap_key="email_code_delivery"))
        else:
            # Always say what the server can and cannot do, so a client never
            # has to guess whether an email is coming.
            payload["email_delivery"] = _sent_state(mc)
        payload["verification_required"] = bool(cfg.require_email_verification)
        return jsonify(payload), 201


@bp.post("/login")
def login():
    cfg = current_app.config["NOVA_CFG"]
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

        # Every account has its NOVA instance from the start (and older
        # accounts get theirs at next sign-in), so it is never missing
        # from anything that counts or looks for instances.
        from .api_instance import get_or_create_instance
        get_or_create_instance(s, user.id)
        tokens = _issue_session(s, user, device, ip,
                                request.headers.get("User-Agent", ""))
        record_event(s, "USER_LOGIN", user_id=user.id, device_id=device.id,
                     platform=device.platform, app_version=device.app_version)
        if how == "registered":
            record_event(s, "DEVICE_REGISTERED", user_id=user.id,
                         device_id=device.id, platform=device.platform)
            # Signing in on a machine this account has never used is exactly
            # the event a user needs to hear about if it was not them.
            _notify_new_device(user.email, device)

        prof = s.get(Profile, user.id)
        payload = {
            "ok": True,
            "user": {"id": user.id, "email": user.email,
                     "display_name": prof.display_name if prof else "",
                     "email_verified": user.email_verified,
                     "created_at": user.created_at},
            "device": {"id": device.id, "name": device.name},
            **tokens,
        }

        # Signing in on an unverified account is where most people first learn
        # that verification is required -- and it used to be the one entry point
        # that never said so and never sent the mail. The client was told
        # nothing, sent the user to the profile step, and the account guard then
        # refused with "Verify your email address to continue." while no message
        # existed to act on. Both facts belong in this response.
        mc = mail.mailer()
        # The account guard refuses every authenticated route for an unverified
        # account, whatever this policy says -- so verification is "required"
        # whenever the user could actually complete it. Sending and telling the
        # client use the same condition, which is what keeps them consistent.
        payload["verification_required"] = bool(cfg.require_email_verification
                                                or mc.configured)
        payload["email_delivery"] = _sent_state(mc)
        pending = None
        if not user.email_verified and (cfg.require_email_verification or mc.configured):
            token = _issue_email_token(s, user.id, "verify_email", 86400)
            code = sec.new_verification_code()
            s.add(EmailToken(
                user_id=user.id,
                kind="verify_email",
                token_hash="code:" + sec.hash_code(code),
                code_hash=sec.hash_code(code),
                expires_at=now() + 86400,
                code_expires_at=now() + 600,
            ))
            pending = (user.email, token, code)

    # Sent after the transaction commits, and outside it: a six-second provider
    # timeout must not hold a pooled database connection open. A failed send
    # does not fail the sign-in -- the user is signed in and can press Resend.
    if pending:
        address, token, code = pending
        payload.update(_deliver(mc, address, mail.verification_email, token,
                                "verify_email"))
        payload.update(_deliver(mc, address, mail.verification_code_email, code,
                                "verify_email_code", wrap_key="email_code_delivery"))
    return jsonify(payload)


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
    err = require_user(allow_unverified=True)
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
        uid = user.id
    # After the commit, so no request can re-cache the old epoch in between.
    from .auth_guard import invalidate_auth_cache
    invalidate_auth_cache(uid)
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

        mc = mail.mailer()
        cfg = current_app.config["NOVA_CFG"]
        user = s.scalar(select(User).where(User.email == email))
        # Configuration facts only. Deliberately no per-message outcome: a
        # `sent` flag here would be true for real addresses and false for
        # unknown ones, which is exactly the oracle the generic message exists
        # to prevent.
        out: dict = {"ok": True, **_delivery_state(mc)}
        if user is not None and user.status == UserStatus.ACTIVE.value:
            token = _issue_email_token(s, user.id, "reset_password", 3600)
            if mc.configured:
                try:
                    subject, text, html_body = mail.reset_email(mc.cfg, token)
                    mc.send(user.email, subject, text, html_body,
                            kind="reset_password")
                except Exception:
                    pass
            elif not cfg.is_production:
                # Nothing can be delivered, so hand the token back so a
                # developer can finish the flow. Refused in production.
                out["dev_reset_token"] = token
        # Identical response either way: this endpoint must not reveal which
        # addresses have accounts. The delivery keys added above are the same
        # regardless, because they describe the server's configuration rather
        # than whether this particular account exists.
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
        uid = user.id
    from .auth_guard import invalidate_auth_cache
    invalidate_auth_cache(uid)
    return jsonify({"ok": True, "message": "Password updated. Sign in again."})


def _consume_email_token(token: str, kind: str) -> tuple[bool, str]:
    """Redeem a single-use email token. Shared by the API and the web page,
    so a link clicked in a browser and a POST from the desktop behave
    identically."""
    if not token:
        return False, "The link is missing its token."
    with session_scope() as s:
        row = s.scalar(select(EmailToken).where(
            EmailToken.token_hash == sec.token_hash(token),
            EmailToken.kind == kind))
        if row is None or row.used_at is not None or row.expires_at < now():
            return False, ("This link is no longer valid. It may already have "
                           "been used, or it may have expired.")
        user = s.get(User, row.user_id)
        if user is None:
            return False, "This link is no longer valid."
        row.used_at = now()
        if kind == "verify_email":
            user.email_verified = True
            record_event(s, "EMAIL_VERIFIED", user_id=user.id)
            # The authorisation cache keeps the pre-verification flag for up to
            # ten seconds. Without this, someone who has just clicked the link
            # is still refused with "Verify your email address to continue."
            from .auth_guard import invalidate_auth_cache
            invalidate_auth_cache(user.id)
            return True, ("Your email address is confirmed. You can return to "
                          "NOVA and carry on.")
        return True, "Token accepted."


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


@bp.post("/email/resend")
def resend_verification():
    """Ask for another verification email.

    Exists because a first attempt can fail for reasons the user cannot see --
    a provider blip, a typo they have since fixed, or delivery not being
    configured when they signed up.
    """
    from .auth_guard import require_user
    err = require_user(allow_unverified=True)
    if err:
        return err
    with session_scope() as s:
        user = s.get(User, g.user_id)
        if user is None:
            return _fail("Not signed in.", 401, "unauthorised")
        if user.email_verified:
            return jsonify({"ok": True, "already_verified": True})
        try:
            rate_limit(s, f"resend:{user.id}", limit=5, window_s=3600)
        except RateLimited as e:
            resp = _fail("Too many requests. Try again shortly.", 429,
                         "rate_limited")
            return resp[0], resp[1], {"Retry-After": str(e.retry_after)}
        token = _issue_email_token(s, user.id, "verify_email", 86400)
        # Also send a new verification code
        code = sec.new_verification_code()
        s.add(EmailToken(
            user_id=user.id,
            kind="verify_email",
            # token_hash is NOT NULL; a code-only row carries a
            # derived placeholder that cannot collide with a link
            # token (opaque tokens are urlsafe base64: no colon).
            token_hash="code:" + sec.hash_code(code),
            code_hash=sec.hash_code(code),
            expires_at=now() + 86400,
            code_expires_at=now() + 600,  # 10 minutes for code
        ))
        mc = mail.mailer()
        return jsonify({"ok": True,
                        **_deliver(mc, user.email, mail.verification_email,
                                   token, "verify_email"),
                        **_deliver(mc, user.email, mail.verification_code_email,
                                   code, "verify_email_code",
                                   wrap_key="email_code_delivery")})


@bp.post("/email/verify_code")
def verify_email_code():
    """Verify a 6-digit email verification code."""
    body = request.get_json(silent=True) or {}
    code = (body.get("code") or "").strip()
    if not code:
        return _fail("Verification code is required.", 400, "missing_code")
    if not code.isdigit() or len(code) != 6:
        return _fail("Verification code must be a 6-digit number.", 400, "invalid_code")
    with session_scope() as s:
        # A six-digit code is only 10^6 possibilities and lives for ten
        # minutes, so unlimited attempts from one host is a guessing window,
        # not a formality.
        try:
            rate_limit(s, f"verify_code:ip:{sec.hash_ip(_client_ip())}",
                       limit=20, window_s=3600)
        except RateLimited as e:
            resp = _fail("Too many attempts. Ask for a new code in a little "
                         "while.", 429, "rate_limited")
            return resp[0], resp[1], {"Retry-After": str(e.retry_after)}
        row = s.scalar(select(EmailToken).where(
            EmailToken.code_hash == sec.hash_code(code),
            EmailToken.kind == "verify_email"))
        if row is None or row.used_at is not None or row.code_expires_at is not None and row.code_expires_at < now():
            return _fail("This verification code is no longer valid. It may already have "
                         "been used, or it may have expired.", 400, "invalid_code")
        user = s.get(User, row.user_id)
        if user is None:
            return _fail("This verification code is no longer valid.", 400, "invalid_code")
        row.used_at = now()
        user.email_verified = True
        record_event(s, "EMAIL_VERIFIED", user_id=user.id)
        # Same reason as the link path: a cached "unverified" must not survive
        # the verification it contradicts.
        from .auth_guard import invalidate_auth_cache
        invalidate_auth_cache(user.id)
        return jsonify({"ok": True, "message": "Your email address is confirmed. You can return to NOVA and carry on."})


@bp.post("/email/resend_code")
def resend_verification_code():
    """Resend a 6-digit verification code via email."""
    from .auth_guard import require_user
    err = require_user(allow_unverified=True)
    if err:
        return err
    with session_scope() as s:
        user = s.get(User, g.user_id)
        if user is None:
            return _fail("Not signed in.", 401, "unauthorised")
        if user.email_verified:
            return jsonify({"ok": True, "already_verified": True})
        try:
            rate_limit(s, f"resend_code:{user.id}", limit=5, window_s=3600)
        except RateLimited as e:
            resp = _fail("Too many requests. Try again shortly.", 429, "rate_limited")
            return resp[0], resp[1], {"Retry-After": str(e.retry_after)}
        code = sec.new_verification_code()
        s.add(EmailToken(
            user_id=user.id,
            kind="verify_email",
            # token_hash is NOT NULL; a code-only row carries a
            # derived placeholder that cannot collide with a link
            # token (opaque tokens are urlsafe base64: no colon).
            token_hash="code:" + sec.hash_code(code),
            code_hash=sec.hash_code(code),
            expires_at=now() + 86400,
            code_expires_at=now() + 600,  # 10 minutes for code
        ))
        mc = mail.mailer()
        return jsonify({"ok": True,
                        **_deliver(mc, user.email, mail.verification_code_email, code, "verify_email_code")})


# -- Google OAuth --------------------------------------------------------------

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

GOOGLE_SCOPES = [
    "openid",
    "email",
    "profile",
]


def _google_oauth_configured() -> tuple[bool, str]:
    """Check if Google OAuth is configured. Returns (configured, error_message)."""
    cfg = current_app.config["NOVA_CFG"]
    if not cfg.google_oauth_client_id:
        return False, "Google OAuth client ID not configured (NOVA_GOOGLE_OAUTH_CLIENT_ID)"
    if not cfg.google_oauth_client_secret:
        return False, "Google OAuth client secret not configured (NOVA_GOOGLE_OAUTH_CLIENT_SECRET)"
    if not cfg.google_oauth_redirect_uri:
        return False, "Google OAuth redirect URI not configured (NOVA_GOOGLE_OAUTH_REDIRECT_URI)"
    return True, ""


#: The state parameter is how the callback proves the code came from an
#: authorization flow *we* started. It used to be generated, attached, and
#: then never checked -- which is the login-CSRF classic: an attacker sends
#: the victim a callback carrying the attacker's code, and the victim's
#: device signs into the attacker's account. The state is now signed with
#: the server's own key and carries its issue time, so the callback verifies
#: both authorship and freshness without a shared session store.
_GOOGLE_STATE_TTL_S = 600


#: Where the browser may be sent after Google answers. The desktop is the
#: only client that can finish a sign-in (it must exchange the code and adopt
#: the session), so the only address this service will ever navigate to is
#: the desktop's own loopback callback. Anything else -- https, a LAN address,
#: a different path, embedded credentials -- is refused before it is signed.
_DESKTOP_CALLBACK_PATH = "/api/account/google/callback"


def _loopback_redirect(value: str) -> str:
    """The redirect if it is a loopback address for the desktop, else "".

    This value comes from a client and later becomes a browser navigation
    carrying an authorization code, so it is allow-listed by *shape*, not
    trusted: an open redirect here would hand a code to whoever asked.
    """
    raw = (value or "").strip()
    if not raw or len(raw) > 300:
        return ""
    try:
        u = urlsplit(raw)
    except ValueError:
        return ""
    if u.scheme != "http" or u.hostname not in ("127.0.0.1", "localhost", "::1"):
        return ""
    if u.port is None or u.username or u.password or u.query or u.fragment:
        return ""
    if u.path != _DESKTOP_CALLBACK_PATH:
        return ""
    return raw


def _google_oauth_state(redirect_to: str = "") -> str:
    """Issue a signed OAuth state: `<v>.<ts>.<nonce>.<redirect64>.<hmac>`.

    The nonce makes every issued state unique even within one second, so a
    captured state cannot be replayed by racing a second flow. The redirect
    (empty when the flow was not started by the desktop) is signed with the
    rest of the state, which is what lets the browser-facing GET callback
    send the code home without becoming an open redirect.
    """
    ts = str(int(time.time()))
    nonce = secrets.token_urlsafe(8)
    r64 = base64.urlsafe_b64encode(
        (redirect_to or "").encode("utf-8")).decode("ascii").rstrip("=")
    key = current_app.config["NOVA_CFG"].secret_key.encode("utf-8")
    sig = _hmac.new(key, f"v1.{ts}.{nonce}.{r64}".encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]
    return f"v1.{ts}.{nonce}.{r64}.{sig}"


def _google_state_parts(state: str) -> tuple[str, str] | None:
    """`(timestamp, redirect64)` when the state's signature and age hold."""
    try:
        version, ts_s, nonce, r64, sig = (state or "").split(".", 4)
        ts = int(ts_s)
    except (ValueError, AttributeError):
        return None
    if version != "v1" or not nonce or not sig:
        return None
    if time.time() - ts > _GOOGLE_STATE_TTL_S:
        return None
    key = current_app.config["NOVA_CFG"].secret_key.encode("utf-8")
    expect = _hmac.new(key, f"v1.{ts_s}.{nonce}.{r64}".encode("utf-8"),
                       hashlib.sha256).hexdigest()[:32]
    if not _hmac.compare_digest(sig, expect):
        return None
    return ts_s, r64


def _google_state_valid(state: str) -> bool:
    return _google_state_parts(state) is not None


def _google_state_redirect(state: str) -> str:
    """The desktop callback signed into this state, re-validated on the way out."""
    parts = _google_state_parts(state)
    if not parts:
        return ""
    r64 = parts[1]
    try:
        raw = base64.urlsafe_b64decode(
            r64 + "=" * (-len(r64) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""
    return _loopback_redirect(raw)


def _oauth_page(title: str, message: str, status: int = 200):
    """A small, script-free page for the browser that Google sent back.

    It is the last thing a person sees before returning to NOVA, so it says
    what happened in their words. Nothing from the query string is echoed:
    the code and state are credentials, and an error page is not the place
    to reflect them.
    """
    from flask import Response
    from html import escape
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)} - NOVA</title>
<style>
 body{{margin:0;min-height:100vh;display:grid;place-items:center;
      background:#0b0d12;color:#e8eaf0;
      font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}}
 main{{max-width:30rem;padding:2rem;text-align:center}}
 h1{{font-size:1.25rem;font-weight:600;margin:0 0 .6rem}}
 p{{margin:0;color:#a6adbb}}
</style></head>
<body><main><h1>{escape(title)}</h1><p>{escape(message)}</p></main></body></html>"""
    return Response(body, status=status, mimetype="text/html",
                    headers={"Cache-Control": "no-store"})


@bp.get("/google/start")
def google_oauth_start():
    """Start Google OAuth flow. Returns authorization URL for the desktop to open in browser.

    Scope is deliberately the minimum sign-in set (who you are, nothing
    else). Gmail, Calendar and Drive are separate, individually-granted
    connections -- signing in grants none of them.
    """
    configured, err = _google_oauth_configured()
    if not configured:
        return _fail(err, 503, "oauth_not_configured")

    cfg = current_app.config["NOVA_CFG"]

    # The desktop passes its own loopback callback so Google's browser
    # redirect can come home to the app that started the flow. A plain
    # browser client passes nothing and gets a state with no redirect; the
    # GET callback then explains that sign-in has to be started from NOVA.
    body = request.get_json(silent=True) or {}
    requested = (request.args.get("client_redirect")
                 or body.get("client_redirect") or "").strip()
    redirect_to = ""
    if requested:
        redirect_to = _loopback_redirect(requested)
        if not redirect_to:
            return _fail("The desktop's sign-in address is not valid.", 400,
                         "invalid_redirect")
    state = _google_oauth_state(redirect_to)

    params = {
        "client_id": cfg.google_oauth_client_id,
        "redirect_uri": cfg.google_oauth_redirect_uri,
        "response_type": "code",
        "scope": " ".join(GOOGLE_SCOPES),
        "state": state,
        # No access_type=offline: basic sign-in does not need a refresh
        # token, and asking for one muddles "who are you" with "let me act
        # when you are absent". Service connections request their own.
        "prompt": "select_account",
    }
    auth_url = f"{GOOGLE_AUTH_URL}?{urlencode(params)}"
    return jsonify({"ok": True, "auth_url": auth_url, "state": state})


@bp.get("/google/callback")
def google_oauth_get_callback():
    """Google's browser redirect for a desktop sign-in.

    Google navigates the browser here (a GET) with the code. The code is
    only useful to the client that holds the secret, so it is not exchanged
    here: the signed state carries the desktop's loopback address, and the
    browser is sent straight back to the app that started the flow. That app
    then calls the POST callback, which exchanges the code and issues the
    session. Keeping the exchange in one place is also what keeps the
    login-CSRF check there authoritative.
    """
    denied = (request.args.get("error") or "").strip()
    if denied:
        message = ("Google sign-in was cancelled."
                   if denied == "access_denied"
                   else "Google sign-in was not completed.")
        return _oauth_page("Sign-in cancelled", message, 400)

    state = (request.args.get("state") or "").strip()
    if not _google_state_valid(state):
        # Forged, replayed, stale -- or a flow started before the desktop
        # handoff existed. None of those may navigate anywhere.
        return _oauth_page(
            "Sign-in expired",
            "This sign-in attempt has expired. Return to NOVA and try again.",
            400)

    target = _google_state_redirect(state)
    if not target:
        return _oauth_page(
            "Open NOVA to finish",
            "This sign-in was not started by the NOVA app. Open NOVA and "
            "choose Continue with Google there.",
            400)

    code = (request.args.get("code") or "").strip()
    if not code:
        return _oauth_page(
            "Sign-in failed", "Google did not return a sign-in code.", 400)

    sep = "&" if "?" in target else "?"
    return redirect(f"{target}{sep}code={quote(code, safe='')}"
                    f"&state={quote(state, safe='')}", code=302)


@bp.post("/google/callback")
def google_oauth_callback():
    """Handle Google OAuth callback. Exchanges code for tokens, creates or links account."""
    body = request.get_json(silent=True) or {}
    # Google itself may deliver the outcome of a declined consent. Fail
    # safely and clearly rather than falling into "missing code".
    denied = (body.get("error") or "").strip()
    if denied:
        return _fail(
            "Google sign-in was not completed"
            + (" (access was denied on the consent screen)." if denied == "access_denied" else "."),
            400, "authorization_denied" if denied == "access_denied" else "oauth_error")
    code = (body.get("code") or "").strip()
    state = (body.get("state") or "").strip()
    device_id = (body.get("device_id") or "").strip()[:36]
    device_secret = (body.get("device_secret") or "").strip()
    platform = (body.get("platform") or "unknown").strip()[:32]
    version = (body.get("app_version") or "").strip()[:32]
    device_name = (body.get("device_name") or "").strip()[:120] or "NOVA device"

    if not code:
        return _fail("Authorization code is required", 400, "missing_code")
    if not state:
        return _fail("OAuth state is required", 400, "missing_state")
    if not _google_state_valid(state):
        # Forged, replayed, or stale. Never proceed on it.
        return _fail("This sign-in attempt has expired. Start again.", 400,
                     "invalid_state")

    configured, err = _google_oauth_configured()
    if not configured:
        return _fail(err, 503, "oauth_not_configured")

    cfg = current_app.config["NOVA_CFG"]

    # Exchange code for tokens
    import requests
    try:
        token_resp = requests.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": cfg.google_oauth_client_id,
                "client_secret": cfg.google_oauth_client_secret,
                "redirect_uri": cfg.google_oauth_redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=10,
        )
        token_resp.raise_for_status()
        tokens = token_resp.json()
    except requests.RequestException as e:
        log.error("[OAUTH] Token exchange failed: %s", e)
        return _fail("Failed to exchange authorization code", 502, "token_exchange_failed")

    access_token = tokens.get("access_token")
    id_token = tokens.get("id_token")  # JWT with user info
    refresh_token = tokens.get("refresh_token")

    if not access_token:
        return _fail("No access token returned from Google", 502, "no_access_token")

    # Get user info from Google
    try:
        userinfo_resp = requests.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        userinfo_resp.raise_for_status()
        userinfo = userinfo_resp.json()
    except requests.RequestException as e:
        log.error("[OAUTH] Userinfo fetch failed: %s", e)
        return _fail("Failed to fetch user information from Google", 502, "userinfo_failed")

    google_sub = userinfo.get("sub")
    email = sec.normalise_email(userinfo.get("email") or "")
    email_verified = userinfo.get("verified_email", False)
    display_name = (userinfo.get("name") or email.split("@")[0] or "").strip()[:120]

    if not google_sub or not email:
        return _fail("Google did not return required user information", 502, "incomplete_userinfo")

    ip = _client_ip()

    with session_scope() as s:
        try:
            rate_limit(s, f"google_oauth:ip:{sec.hash_ip(ip)}", limit=10, window_s=900)
        except RateLimited as e:
            resp = _fail("Too many OAuth attempts. Try again later.", 429, "rate_limited")
            return resp[0], resp[1], {"Retry-After": str(e.retry_after)}

        # Try to find existing user by email
        user = s.scalar(select(User).where(User.email == email))

        if user is None:
            # Create new user
            user = User(
                email=email,
                password_hash="",  # No password for OAuth-only accounts
                email_verified=email_verified,
            )
            s.add(user)
            s.flush()
            s.add(Profile(user_id=user.id, display_name=display_name))
            record_event(s, "USER_CREATED", user_id=user.id, method="google_oauth")
        else:
            # Existing user - update profile if needed
            prof = s.get(Profile, user.id)
            if prof and not prof.display_name:
                prof.display_name = display_name
            # Update email_verified if Google says it's verified
            if email_verified and not user.email_verified:
                user.email_verified = True
                record_event(s, "EMAIL_VERIFIED", user_id=user.id, method="google_oauth")

        # Register device
        try:
            device, how = _register_device(s, user, {
                "device_id": device_id,
                "device_secret": device_secret,
                "platform": platform,
                "app_version": version,
                "device_name": device_name,
            })
        except (ValueError, PermissionError) as e:
            return _fail(str(e), 400, "device_error")

        from .api_instance import get_or_create_instance
        get_or_create_instance(s, user.id)

        auth_session = _issue_session(s, user, device, ip, request.headers.get("User-Agent", ""))

        # Store Google refresh token if provided (for future use)
        # This would need a new column on User or a separate OAuthAccount table
        # For now, we just sign the user in

        record_event(s, "USER_LOGIN", user_id=user.id, device_id=device.id, method="google_oauth")
        if how == "registered":
            record_event(s, "DEVICE_REGISTERED", user_id=user.id, device_id=device.id)

        return jsonify({
            "ok": True,
            "user": {
                "id": user.id,
                "email": user.email,
                "display_name": display_name,
                "email_verified": user.email_verified,
                "created_at": user.created_at,
            },
            "device": {"id": device.id, "name": device.name},
            **auth_session,
        })


@bp.get("/me")
def me():
    from .auth_guard import require_user
    err = require_user(allow_unverified=True)
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
