"""nova_cloud.api_updates — which NOVA version each installation should run.

    GET  /v1/updates/check?version=1.0.0&device_id=...   public
    POST /v1/updates/report                              signed-in device

A release is a manifest (JSON: version, url, sha256, size, min_supported,
notes) plus an Ed25519 signature made with the owner's private key, which
never touches this server. The server checks the signature when a release is
published (so a mismatched pair cannot be stored) and serves both verbatim;
the desktop verifies again with the public key compiled into it. A server that
is compromised can therefore withhold updates, but cannot ship one.

Rollout is scheduling, not integrity, so it lives here and not in the signed
manifest: a device is in a release's rollout when a stable hash of
(device, version) falls below the rollout percentage. `min_supported` always
wins over rollout -- an unsupported client is offered the update regardless.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re

from flask import Blueprint, g, jsonify, request
from sqlalchemy import select

from .auth_guard import user_required
from .db import RateLimited, rate_limit, session_scope
from .models import DeviceUpdateState, Release, now

bp = Blueprint("updates", __name__, url_prefix="/v1/updates")

CHANNELS = ("stable", "beta", "dev")
_VERSION = re.compile(r"^\d+(\.\d+){0,3}$")


def parse_version(v: str) -> tuple:
    v = (v or "").strip().lstrip("v")
    if not _VERSION.match(v):
        raise ValueError(f"not a version: {v!r}")
    parts = [int(x) for x in v.split(".")]
    return tuple(parts + [0] * (4 - len(parts)))


def verify_manifest(manifest: str, signature_b64: str, public_key_b64: str) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        key.verify(base64.b64decode(signature_b64), manifest.encode("utf-8"))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def in_rollout(device_id: str, version: str, percent: int) -> bool:
    if percent >= 100:
        return True
    if percent <= 0 or not device_id:
        return False
    h = hashlib.sha256(f"{device_id}:{version}".encode()).digest()
    return int.from_bytes(h[:2], "big") % 100 < percent


def publish(manifest: str, signature_b64: str, public_key_b64: str, *,
            channel: str = "stable", rollout_percent: int = 100) -> dict:
    """Store a release after proving the signature. Used by manage.py."""
    if channel not in CHANNELS:
        raise ValueError(f"channel must be one of {CHANNELS}")
    if not public_key_b64:
        raise ValueError("NOVA_UPDATE_PUBLIC_KEY is not set")
    if not verify_manifest(manifest, signature_b64, public_key_b64):
        raise ValueError("signature does not match this manifest and public key")
    data = json.loads(manifest)
    for k in ("version", "url", "sha256", "size"):
        if not data.get(k):
            raise ValueError(f"manifest is missing {k!r}")
    parse_version(data["version"])
    min_supported = str(data.get("min_supported") or "0.0.0")
    parse_version(min_supported)
    with session_scope() as s:
        for old in s.scalars(select(Release).where(Release.channel == channel,
                                                   Release.active.is_(True))).all():
            old.active = False
        rel = Release(channel=channel, version=data["version"], manifest=manifest,
                      signature=signature_b64, min_supported=min_supported,
                      rollout_percent=max(0, min(100, int(rollout_percent))))
        s.add(rel)
        s.flush()
        return {"id": rel.id, "channel": channel, "version": rel.version,
                "min_supported": min_supported, "rollout_percent": rel.rollout_percent}


@bp.get("/check")
def check():
    version = (request.args.get("version") or "").strip()
    device_id = (request.args.get("device_id") or "").strip()[:36]
    try:
        current = parse_version(version)
    except ValueError:
        return jsonify({"ok": False, "error": "bad_version"}), 400

    from .auth_guard import client_ip
    from . import security as sec
    with session_scope() as s:
        try:
            rate_limit(s, f"updcheck:ip:{sec.hash_ip(client_ip())}", limit=120, window_s=3600)
        except RateLimited as e:
            return jsonify({"ok": False, "error": "rate_limited"}), 429, \
                {"Retry-After": str(e.retry_after)}

        # The channel is the server's decision for this device (an admin
        # enrols a device in beta); the client cannot opt itself in.
        state = s.get(DeviceUpdateState, device_id) if device_id else None
        channel = state.channel if state else "stable"
        if state is not None:
            state.current_version = version
            state.last_check_at = now()
        rel = s.scalar(select(Release).where(Release.channel == channel,
                                             Release.active.is_(True))
                       .order_by(Release.created_at.desc()))
        if rel is None:
            return jsonify({"ok": True, "channel": channel, "latest": None,
                            "min_supported": "0.0.0", "required": False, "update": None})

        required = current < parse_version(rel.min_supported)
        newer = parse_version(rel.version) > current
        offer = newer and (required or in_rollout(device_id, rel.version, rel.rollout_percent))
        return jsonify({
            "ok": True, "channel": channel, "latest": rel.version,
            "min_supported": rel.min_supported, "required": required,
            "update": {"manifest": rel.manifest, "signature": rel.signature} if offer else None,
        })


@bp.post("/report")
@user_required(allow_unverified=True)
def report():
    body = request.get_json(silent=True) or {}
    result = str(body.get("result") or "")[:16]
    if result not in ("installed", "failed", "rolled_back", "downloaded"):
        return jsonify({"ok": False, "error": "bad_result"}), 400
    with session_scope() as s:
        st = s.get(DeviceUpdateState, g.device_id)
        if st is None:
            st = DeviceUpdateState(device_id=g.device_id)
            s.add(st)
        st.last_result = result
        st.last_target = str(body.get("target") or "")[:32]
        st.current_version = str(body.get("current") or st.current_version or "")[:32]
        st.last_error = (str(body.get("error") or "")[:200] or None)
    return jsonify({"ok": True})


__all__ = ["bp", "parse_version", "verify_manifest", "in_rollout", "publish", "CHANNELS"]
