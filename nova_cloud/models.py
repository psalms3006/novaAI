"""nova_cloud.models — the NOVA platform schema.

Design rules followed here:

  * `user_id` and `device_id` are opaque UUIDs and never change. Email is a
    mutable attribute of an account, not its identity, and a device identity is
    independent of hostname, OS user, MAC or IP -- all of which change.
  * Secrets are stored hashed, never in plaintext: passwords with Argon2id,
    tokens with SHA-256 (they are already high-entropy, so a slow KDF buys
    nothing and would make every request expensive).
  * Telemetry tables hold operational metadata only. There is deliberately no
    column anywhere in this file for conversation text, transcripts or audio.
"""
from __future__ import annotations

import enum
import time
import uuid

from sqlalchemy import (
    Boolean, Column, Float, ForeignKey, Index, Integer, JSON, String, Text,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


def now() -> float:
    return time.time()


# -- identity ----------------------------------------------------------------

class UserStatus(str, enum.Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    DELETED = "deleted"


class User(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=_uuid)
    # Stored lowercase; uniqueness is enforced on the normalised form so
    # two casings of the same address cannot both exist.
    email = Column(String(320), nullable=False, unique=True, index=True)
    email_verified = Column(Boolean, nullable=False, default=False)
    password_hash = Column(Text, nullable=False)
    status = Column(String(16), nullable=False, default=UserStatus.ACTIVE.value)

    created_at = Column(Float, nullable=False, default=now)
    updated_at = Column(Float, nullable=False, default=now, onupdate=now)
    last_active_at = Column(Float, nullable=True)
    deleted_at = Column(Float, nullable=True)

    # Bumped on password change and on "sign out everywhere". Access tokens
    # carry this value, so raising it invalidates every outstanding token at
    # once without a database lookup on the hot path.
    token_epoch = Column(Integer, nullable=False, default=0)

    profile = relationship("Profile", back_populates="user", uselist=False,
                           cascade="all, delete-orphan")
    devices = relationship("Device", back_populates="user",
                           cascade="all, delete-orphan")


class Profile(Base):
    __tablename__ = "profiles"

    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"),
                     primary_key=True)
    display_name = Column(String(120), nullable=False, default="")
    locale = Column(String(16), nullable=False, default="en")
    avatar_url = Column(Text, nullable=True)
    created_at = Column(Float, nullable=False, default=now)
    updated_at = Column(Float, nullable=False, default=now, onupdate=now)

    user = relationship("User", back_populates="profile")


# -- devices and sessions ----------------------------------------------------

class Device(Base):
    __tablename__ = "devices"

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    name = Column(String(120), nullable=False, default="NOVA device")
    platform = Column(String(32), nullable=False, default="unknown")
    app_version = Column(String(32), nullable=False, default="")
    # The device proves itself with a secret it generated locally; only the
    # hash is kept, so a database leak does not let anyone impersonate devices.
    secret_hash = Column(String(64), nullable=False)

    created_at = Column(Float, nullable=False, default=now)
    last_seen_at = Column(Float, nullable=True)
    revoked_at = Column(Float, nullable=True)

    user = relationship("User", back_populates="devices")

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None


class AuthSession(Base):
    """One refresh-token family: one signed-in device."""

    __tablename__ = "auth_sessions"

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    device_id = Column(String(36), ForeignKey("devices.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    refresh_hash = Column(String(64), nullable=False, unique=True, index=True)

    issued_at = Column(Float, nullable=False, default=now)
    expires_at = Column(Float, nullable=False)
    last_used_at = Column(Float, nullable=True)
    revoked_at = Column(Float, nullable=True)
    # Set when an already-rotated token is presented again. That means the
    # token leaked, so the whole family is killed.
    reuse_detected_at = Column(Float, nullable=True)

    ip_hash = Column(String(64), nullable=True)
    user_agent = Column(String(200), nullable=True)


class EmailToken(Base):
    """Email verification and password reset. Single use, short lived."""

    __tablename__ = "email_tokens"

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    kind = Column(String(24), nullable=False)        # verify_email, reset_password
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    created_at = Column(Float, nullable=False, default=now)
    expires_at = Column(Float, nullable=False)
    used_at = Column(Float, nullable=True)


# -- synced preferences ------------------------------------------------------

class Preference(Base):
    """Cloud-synced user preference, one row per key.

    Conflict resolution is last-write-wins on `updated_at`, with the writing
    device recorded. That is appropriate for scalar UI and voice preferences.
    Anything needing stronger guarantees does not belong in this table.
    """

    __tablename__ = "preferences"

    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"),
                     primary_key=True)
    key = Column(String(64), primary_key=True)
    value = Column(JSON, nullable=True)
    updated_at = Column(Float, nullable=False, default=now)
    updated_by_device = Column(String(36), nullable=True)
    version = Column(Integer, nullable=False, default=1)


# -- observability (metadata only) -------------------------------------------

class ActivityEvent(Base):
    __tablename__ = "activity_events"

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=True, index=True)
    device_id = Column(String(36), nullable=True, index=True)
    type = Column(String(48), nullable=False, index=True)
    ts = Column(Float, nullable=False, default=now, index=True)
    app_version = Column(String(32), nullable=True)
    platform = Column(String(32), nullable=True)
    # Bounded metadata, sanitised on ingest. See api_telemetry.
    attrs = Column(JSON, nullable=True)


Index("ix_activity_user_ts", ActivityEvent.user_id, ActivityEvent.ts)


class ModelCall(Base):
    """Per-request provider metrics. No prompts, no completions."""

    __tablename__ = "model_calls"

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String(36), nullable=True, index=True)
    device_id = Column(String(36), nullable=True)
    provider = Column(String(48), nullable=False, index=True)
    model = Column(String(96), nullable=False)
    ts = Column(Float, nullable=False, default=now, index=True)
    latency_ms = Column(Integer, nullable=True)
    first_token_ms = Column(Integer, nullable=True)
    status = Column(String(16), nullable=False, default="success")
    error_code = Column(String(48), nullable=True)
    tokens_in = Column(Integer, nullable=True)
    tokens_out = Column(Integer, nullable=True)
    offline = Column(Boolean, nullable=False, default=False)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String(36), nullable=True, index=True)
    device_id = Column(String(36), nullable=True)
    agent = Column(String(64), nullable=False, index=True)
    task_type = Column(String(64), nullable=True)
    started_at = Column(Float, nullable=False, default=now, index=True)
    ended_at = Column(Float, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    status = Column(String(16), nullable=False, default="running")
    model = Column(String(96), nullable=True)
    error_code = Column(String(48), nullable=True)


class ErrorEvent(Base):
    __tablename__ = "error_events"

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String(36), nullable=True, index=True)
    device_id = Column(String(36), nullable=True)
    code = Column(String(48), nullable=False, index=True)
    ts = Column(Float, nullable=False, default=now, index=True)
    app_version = Column(String(32), nullable=True)
    platform = Column(String(32), nullable=True)
    context = Column(JSON, nullable=True)


# -- feature flags -----------------------------------------------------------

class FeatureFlag(Base):
    __tablename__ = "feature_flags"

    key = Column(String(64), primary_key=True)
    description = Column(Text, nullable=False, default="")
    enabled = Column(Boolean, nullable=False, default=False)
    rollout_percent = Column(Integer, nullable=False, default=0)
    updated_at = Column(Float, nullable=False, default=now, onupdate=now)


class FeatureFlagOverride(Base):
    __tablename__ = "feature_flag_overrides"

    flag_key = Column(String(64),
                      ForeignKey("feature_flags.key", ondelete="CASCADE"),
                      primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"),
                     primary_key=True)
    enabled = Column(Boolean, nullable=False)


# -- administration ----------------------------------------------------------

class AdminRole(str, enum.Enum):
    SUPER_ADMIN = "SUPER_ADMIN"
    ADMIN = "ADMIN"
    SUPPORT = "SUPPORT"
    ANALYST = "ANALYST"


class AdminUser(Base):
    __tablename__ = "admin_users"

    id = Column(String(36), primary_key=True, default=_uuid)
    email = Column(String(320), nullable=False, unique=True, index=True)
    password_hash = Column(Text, nullable=False)
    role = Column(String(24), nullable=False, default=AdminRole.ANALYST.value)
    status = Column(String(16), nullable=False, default="active")
    # Encrypted with the admin signing key; never returned by any endpoint.
    totp_secret_enc = Column(Text, nullable=True)
    mfa_enabled = Column(Boolean, nullable=False, default=False)
    created_at = Column(Float, nullable=False, default=now)
    last_login_at = Column(Float, nullable=True)
    failed_logins = Column(Integer, nullable=False, default=0)
    locked_until = Column(Float, nullable=True)


class AdminSession(Base):
    __tablename__ = "admin_sessions"

    id = Column(String(36), primary_key=True, default=_uuid)
    admin_id = Column(String(36),
                      ForeignKey("admin_users.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    issued_at = Column(Float, nullable=False, default=now)
    expires_at = Column(Float, nullable=False)
    revoked_at = Column(Float, nullable=True)
    ip_hash = Column(String(64), nullable=True)
    user_agent = Column(String(200), nullable=True)


class AdminAuditLog(Base):
    """Every privileged action. Append-only by convention, and never exposed
    for deletion through any endpoint."""

    __tablename__ = "admin_audit_logs"

    id = Column(String(36), primary_key=True, default=_uuid)
    admin_id = Column(String(36), nullable=True, index=True)
    admin_email = Column(String(320), nullable=True)
    action = Column(String(64), nullable=False, index=True)
    target_type = Column(String(32), nullable=True)
    target_id = Column(String(64), nullable=True, index=True)
    ts = Column(Float, nullable=False, default=now, index=True)
    ip_hash = Column(String(64), nullable=True)
    result = Column(String(16), nullable=False, default="success")
    detail = Column(JSON, nullable=True)


class RateLimitBucket(Base):
    """Persistent counters, so limits survive a restart and hold across
    workers when a shared database is configured."""

    __tablename__ = "rate_limits"

    key = Column(String(160), primary_key=True)
    window_start = Column(Float, nullable=False)
    count = Column(Integer, nullable=False, default=0)


__all__ = [
    "Base", "User", "UserStatus", "Profile", "Device", "AuthSession",
    "EmailToken", "Preference", "ActivityEvent", "ModelCall", "AgentRun",
    "ErrorEvent", "FeatureFlag", "FeatureFlagOverride", "AdminUser",
    "AdminRole", "AdminSession", "AdminAuditLog", "RateLimitBucket", "now",
]
