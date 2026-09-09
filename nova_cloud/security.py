"""nova_cloud.security — hashing, tokens, rate limiting and RBAC.

Token design, and why it is this shape:

  * **Access token** — a short-lived signed JWT. Stateless, so the hot path
    costs a signature check rather than a database round trip. It carries the
    user's `token_epoch`, which means "sign out everywhere" and password
    changes invalidate every outstanding token instantly without a lookup.
  * **Refresh token** — an opaque random string, stored only as a SHA-256
    hash, rotated on every use. Rotation gives leak detection: if a token that
    was already exchanged shows up again, the whole family is revoked.

Access tokens are deliberately short (15 minutes by default) because that is
the window in which a revoked device can still act. Revocation takes effect on
the next refresh, and immediately for anything that checks the session.

Admin tokens are signed with a *different* key and carry a different audience,
so a user token can never be replayed against an admin endpoint even if the
route check were somehow bypassed.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .config import config

# OWASP-aligned parameters. Tuned to stay under ~100ms on a modest server:
# login is not a hot path and the cost is what protects a leaked database.
_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)

AUD_USER = "nova:user"
AUD_ADMIN = "nova:admin"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")


# -- passwords ---------------------------------------------------------------

class PasswordPolicyError(ValueError):
    pass


def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def valid_email(email: str) -> bool:
    e = normalise_email(email)
    return bool(e) and len(e) <= 320 and bool(_EMAIL_RE.match(e))


def check_password_policy(password: str) -> None:
    """Length first, because length is what actually matters.

    Deliberately no "must contain a symbol" rule: it pushes people towards
    predictable substitutions without adding real entropy.
    """
    if password is None or len(password) < 10:
        raise PasswordPolicyError("Password must be at least 10 characters.")
    if len(password) > 1024:
        raise PasswordPolicyError("Password is too long.")
    lowered = password.lower()
    for bad in ("password", "12345678", "qwerty", "letmein", "nova1234"):
        if bad in lowered:
            raise PasswordPolicyError("Password is too easy to guess.")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        _hasher.verify(stored_hash, password)
        return True
    except (VerifyMismatchError, InvalidHashError, Exception):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except Exception:
        return False


# -- opaque tokens -----------------------------------------------------------

def new_opaque_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    """SHA-256. The token is already 256 bits of entropy, so a slow KDF adds
    nothing here and would make every authenticated request expensive."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def hash_ip(ip: str | None) -> str | None:
    """Store a keyed hash rather than the address itself.

    Enough to spot 'these logins came from the same place' without keeping a
    log of where the user physically is.
    """
    if not ip:
        return None
    key = config().secret_key.encode("utf-8")
    return hmac.new(key, ip.encode("utf-8"), hashlib.sha256).hexdigest()


# -- JWT access tokens -------------------------------------------------------

def mint_access_token(*, user_id: str, device_id: str, session_id: str,
                      token_epoch: int, ttl_s: int | None = None) -> tuple[str, int]:
    cfg = config()
    ttl = int(ttl_s or cfg.access_ttl_s)
    iat = int(time.time())
    payload = {
        "sub": user_id,
        "did": device_id,
        "sid": session_id,
        "ep": int(token_epoch),
        "aud": AUD_USER,
        "iat": iat,
        "exp": iat + ttl,
    }
    return jwt.encode(payload, cfg.secret_key, algorithm="HS256"), ttl


def verify_access_token(token: str) -> dict[str, Any]:
    """Raises jwt exceptions on anything invalid. Callers must not swallow
    them into a generic success path."""
    return jwt.decode(token, config().secret_key, algorithms=["HS256"],
                      audience=AUD_USER)


def mint_admin_token(*, admin_id: str, role: str, session_id: str,
                     ttl_s: int | None = None) -> tuple[str, int]:
    cfg = config()
    ttl = int(ttl_s or cfg.admin_session_ttl_s)
    iat = int(time.time())
    payload = {
        "sub": admin_id,
        "role": role,
        "sid": session_id,
        "aud": AUD_ADMIN,
        "iat": iat,
        "exp": iat + ttl,
    }
    # Separate key: a user token must never validate here.
    return jwt.encode(payload, cfg.admin_secret_key, algorithm="HS256"), ttl


def verify_admin_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, config().admin_secret_key, algorithms=["HS256"],
                      audience=AUD_ADMIN)


# -- TOTP secret storage -----------------------------------------------------

def _totp_key() -> bytes:
    return hashlib.sha256(("totp:" + config().admin_secret_key).encode()).digest()


def encrypt_totp_secret(secret: str) -> str:
    """Keystream-encrypt the TOTP seed so a database dump alone does not let
    an attacker generate an admin's second factor."""
    key = _totp_key()
    nonce = secrets.token_bytes(16)
    data = secret.encode("utf-8")
    stream = b""
    counter = 0
    while len(stream) < len(data):
        stream += hashlib.sha256(key + nonce + counter.to_bytes(4, "big")).digest()
        counter += 1
    ct = bytes(a ^ b for a, b in zip(data, stream))
    mac = hmac.new(key, nonce + ct, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(nonce + mac + ct).decode("ascii")


def decrypt_totp_secret(blob: str) -> str:
    key = _totp_key()
    raw = base64.urlsafe_b64decode(blob.encode("ascii"))
    nonce, mac, ct = raw[:16], raw[16:32], raw[32:]
    expect = hmac.new(key, nonce + ct, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(mac, expect):
        raise ValueError("TOTP secret failed integrity check")
    stream = b""
    counter = 0
    while len(stream) < len(ct):
        stream += hashlib.sha256(key + nonce + counter.to_bytes(4, "big")).digest()
        counter += 1
    return bytes(a ^ b for a, b in zip(ct, stream)).decode("utf-8")


# -- RBAC --------------------------------------------------------------------

# What each admin role may do. Anything not listed is denied: adding a new
# endpoint without granting it explicitly fails closed.
PERMISSIONS: dict[str, set[str]] = {
    "SUPER_ADMIN": {
        "dashboard.view", "users.view", "users.disable", "users.enable",
        "users.revoke_sessions", "users.delete", "devices.view", "devices.revoke",
        "activity.view", "agents.view", "models.view", "health.view",
        "errors.view", "flags.view", "flags.edit", "audit.view",
        "admins.view", "admins.manage",
    },
    "ADMIN": {
        "dashboard.view", "users.view", "users.disable", "users.enable",
        "users.revoke_sessions", "devices.view", "devices.revoke",
        "activity.view", "agents.view", "models.view", "health.view",
        "errors.view", "flags.view", "flags.edit", "audit.view",
    },
    "SUPPORT": {
        "dashboard.view", "users.view", "users.revoke_sessions",
        "devices.view", "devices.revoke", "activity.view", "health.view",
        "errors.view",
    },
    "ANALYST": {
        "dashboard.view", "activity.view", "agents.view", "models.view",
        "health.view", "errors.view", "flags.view",
    },
}


def role_can(role: str, permission: str) -> bool:
    return permission in PERMISSIONS.get((role or "").upper(), set())


__all__ = [
    "PasswordPolicyError", "normalise_email", "valid_email",
    "check_password_policy", "hash_password", "verify_password", "needs_rehash",
    "new_opaque_token", "token_hash", "hash_ip",
    "mint_access_token", "verify_access_token",
    "mint_admin_token", "verify_admin_token",
    "encrypt_totp_secret", "decrypt_totp_secret",
    "PERMISSIONS", "role_can", "AUD_USER", "AUD_ADMIN",
]
