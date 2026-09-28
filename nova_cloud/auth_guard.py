"""nova_cloud.auth_guard — request authorisation.

Every protected endpoint goes through here. Two rules matter most:

  1. Identity comes from the *token*, never from the request body. A client
     that sends `{"user_id": "..."}` is ignored; `g.user_id` is whatever the
     signature says and nothing else. This is what stops one user reading
     another's data by editing a payload.
  2. A token is not enough on its own. The user must still be active, the
     device must not be revoked, and the token's epoch must match the account's
     current epoch, so "sign out everywhere" and device revocation take effect
     without waiting for expiry.
"""
from __future__ import annotations

import functools
import time
from typing import Callable

import jwt
from flask import g, jsonify, request

from . import security as sec
from .db import session_scope
from .models import AdminSession, AdminUser, Device, User, UserStatus

# Revocation state changes rarely and is read on every request, so a tiny TTL
# cache keeps the common path to one signature check without letting a
# revocation sit unnoticed for long.
_REVOCATION_TTL_S = 10.0
_cache: dict[str, tuple[float, tuple]] = {}


def client_ip() -> str:
    """The caller's address, for rate limits and hashed audit fields.

    X-Forwarded-For is written by whoever sends the request, so only the
    entries appended by our own proxies can be believed: with N trusted hops,
    the Nth entry from the right is the address the outermost proxy saw.
    With no configured proxy the header is ignored.
    """
    from .config import config
    hops = max(0, int(config().trusted_proxy_hops))
    if hops:
        parts = [p.strip() for p in request.headers.get("X-Forwarded-For", "").split(",")
                 if p.strip()]
        if len(parts) >= hops:
            return parts[-hops]
    return request.remote_addr or ""


def _unauthorised(message: str, code: str = "unauthorised", status: int = 401):
    return jsonify({"ok": False, "error": code, "message": message}), status


def _bearer() -> str:
    h = request.headers.get("Authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return ""


def _account_state(user_id: str, device_id: str):
    """(status, token_epoch, device_revoked) with a short cache."""
    key = f"{user_id}:{device_id}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _REVOCATION_TTL_S:
        return hit[1]
    with session_scope() as s:
        user = s.get(User, user_id)
        dev = s.get(Device, device_id) if device_id else None
        state = (
            user.status if user else None,
            int(user.token_epoch) if user else -1,
            (dev.revoked_at is not None) if dev else True,
            bool(user.email_verified) if user else False,
        )
    _cache[key] = (time.time(), state)
    return state


def invalidate_auth_cache(user_id: str | None = None) -> None:
    """Called after revocation so it takes effect immediately, not in 10s."""
    if user_id is None:
        _cache.clear()
        return
    for k in [k for k in _cache if k.startswith(user_id + ":")]:
        _cache.pop(k, None)


def require_user(allow_unverified: bool = False):
    """Populate g.user_id / g.device_id, or return an error response.

    Returns None on success so callers can write `if err: return err`.
    An account whose email is not verified reaches only the routes that
    pass `allow_unverified` (who am I, resend, sign out, delete).
    """
    token = _bearer()
    if not token:
        return _unauthorised("Sign in to continue.", "missing_token")
    try:
        claims = sec.verify_access_token(token)
    except jwt.ExpiredSignatureError:
        return _unauthorised("Session expired.", "token_expired")
    except jwt.InvalidTokenError:
        return _unauthorised("Invalid session.", "invalid_token")

    user_id = claims.get("sub") or ""
    device_id = claims.get("did") or ""
    status, epoch, device_revoked, email_verified = _account_state(user_id, device_id)

    if status is None:
        return _unauthorised("Invalid session.", "invalid_token")
    if status != UserStatus.ACTIVE.value:
        return _unauthorised("This account is not active.", "account_disabled", 403)
    if int(claims.get("ep", -1)) != epoch:
        # Password change or sign-out-everywhere happened after this token was
        # minted.
        return _unauthorised("Session expired.", "token_expired")
    if device_revoked:
        return _unauthorised("This device has been signed out.", "device_revoked", 403)
    if not email_verified and not allow_unverified:
        return _unauthorised("Verify your email address to continue.",
                             "email_unverified", 403)

    g.user_id = user_id
    g.device_id = device_id
    g.session_id = claims.get("sid") or ""
    return None


def user_required(fn: Callable | None = None, *, allow_unverified: bool = False):
    """`@user_required` or `@user_required(allow_unverified=True)`."""
    def deco(f: Callable):
        @functools.wraps(f)
        def wrapper(*a, **kw):
            err = require_user(allow_unverified=allow_unverified)
            if err:
                return err
            return f(*a, **kw)
        return wrapper
    return deco(fn) if fn is not None else deco


# -- admin -------------------------------------------------------------------

def require_admin(permission: str | None = None):
    """Verify an admin token and, optionally, a specific permission.

    Admin tokens are signed with a separate key and carry a separate audience,
    so a user token cannot reach here even if a route were mis-registered.
    """
    token = _bearer()
    if not token:
        return _unauthorised("Administrator sign-in required.", "missing_token")
    try:
        claims = sec.verify_admin_token(token)
    except jwt.ExpiredSignatureError:
        return _unauthorised("Administrator session expired.", "token_expired")
    except jwt.InvalidTokenError:
        return _unauthorised("Invalid administrator session.", "invalid_token")

    admin_id = claims.get("sub") or ""
    session_id = claims.get("sid") or ""
    with session_scope() as s:
        admin = s.get(AdminUser, admin_id)
        if admin is None or admin.status != "active":
            return _unauthorised("Administrator account is not active.",
                                 "admin_disabled", 403)
        # Privileged sessions are checked against the database on every
        # request, not just trusted from the signature. Signing out, or an
        # operator killing a session, has to take effect immediately -- 30
        # minutes of residual admin access is not acceptable.
        row = s.get(AdminSession, session_id) if session_id else None
        if row is None or row.revoked_at is not None or row.expires_at < time.time():
            return _unauthorised("Administrator session expired.",
                                 "session_revoked")
        role = admin.role
        email = admin.email

    # The role is re-read from the database, never trusted from the token: a
    # demotion must take effect on the next request.
    if permission and not sec.role_can(role, permission):
        return jsonify({"ok": False, "error": "forbidden",
                        "message": f"Your role ({role}) cannot perform this action."}), 403

    g.admin_id = admin_id
    g.admin_role = role
    g.admin_email = email
    g.admin_session_id = claims.get("sid") or ""
    return None


def admin_required(permission: str | None = None):
    def deco(fn: Callable):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            err = require_admin(permission)
            if err:
                return err
            return fn(*a, **kw)
        return wrapper
    return deco


__all__ = ["require_user", "user_required", "require_admin", "admin_required",
           "invalidate_auth_cache", "client_ip"]
