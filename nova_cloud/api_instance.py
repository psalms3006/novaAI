"""nova_cloud.api_instance — the account's personal NOVA.

    GET   /v1/instance                      profile, onboarding state, plan, usage
    PATCH /v1/instance/profile              preferred name, role, about
    POST  /v1/instance/onboarding/complete  mark setup finished (idempotent)
    GET   /v1/models/offline                recommended offline model catalog

The instance is always resolved from the token's user, never from anything in
the request, so no client can read or change another person's NOVA.
Onboarding state lives here, on the server, so it survives reinstalls, updates
and new devices: a second PC for the same account skips the profile step.
"""
from __future__ import annotations

import json
import os
import time

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import select

from .auth_guard import user_required
from .db import session_scope
from .models import Instance, ModelUsage, Profile, now

bp = Blueprint("instance", __name__)

ROLES = ("student", "developer", "researcher", "business_owner", "professional", "other")


def today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def get_or_create_instance(s, user_id: str) -> Instance:
    inst = s.scalar(select(Instance).where(Instance.user_id == user_id))
    if inst is None:
        prof = s.get(Profile, user_id)
        inst = Instance(user_id=user_id,
                        preferred_name=(prof.display_name if prof else "")[:80])
        s.add(inst)
        s.flush()
    return inst


def usage_today(s, instance_id: str) -> dict:
    rows = s.scalars(select(ModelUsage).where(ModelUsage.instance_id == instance_id,
                                              ModelUsage.day == today())).all()
    return {r.kind: r.requests for r in rows}


def _public(inst: Instance, s) -> dict:
    cfg = current_app.config["NOVA_CFG"]
    return {
        "id": inst.id,
        "profile": {"preferred_name": inst.preferred_name, "role": inst.role,
                    "about": inst.about},
        "onboarding_completed": inst.onboarding_completed_at is not None,
        "onboarding_completed_at": inst.onboarding_completed_at,
        "plan": inst.plan,
        "limits": cfg.plan_limits(inst.plan),
        "usage_today": usage_today(s, inst.id),
        "created_at": inst.created_at,
    }


@bp.get("/v1/instance")
@user_required
def get_instance():
    with session_scope() as s:
        inst = get_or_create_instance(s, g.user_id)
        return jsonify({"ok": True, "instance": _public(inst, s)})


@bp.patch("/v1/instance/profile")
@user_required
def patch_profile():
    body = request.get_json(silent=True) or {}
    with session_scope() as s:
        inst = get_or_create_instance(s, g.user_id)
        if "preferred_name" in body:
            inst.preferred_name = str(body.get("preferred_name") or "").strip()[:80]
            prof = s.get(Profile, g.user_id)
            if prof is not None and inst.preferred_name:
                prof.display_name = inst.preferred_name
        if "role" in body:
            role = str(body.get("role") or "").strip().lower()
            if role and role not in ROLES:
                return jsonify({"ok": False, "error": "invalid_role",
                                "message": f"role must be one of {', '.join(ROLES)}"}), 400
            inst.role = role
        if "about" in body:
            inst.about = str(body.get("about") or "").strip()[:4000]
        inst.updated_at = now()
        return jsonify({"ok": True, "instance": _public(inst, s)})


@bp.post("/v1/instance/onboarding/complete")
@user_required
def complete_onboarding():
    with session_scope() as s:
        inst = get_or_create_instance(s, g.user_id)
        if inst.onboarding_completed_at is None:
            inst.onboarding_completed_at = now()
        return jsonify({"ok": True, "instance": _public(inst, s)})


# -- offline model catalog -----------------------------------------------------
#
# The owner can replace this without a release by pointing
# NOVA_OFFLINE_CATALOG at a JSON file with the same shape. Sizes are what the
# desktop shows before asking; Ollama verifies each downloaded layer against
# its sha256 digest, and the installer is checked against `sha256` here.

DEFAULT_OFFLINE_CATALOG = {
    "runtime": {
        "name": "ollama",
        "installer_url": "https://ollama.com/download/OllamaSetup.exe",
        # Empty means "not pinned": the desktop refuses to run an unpinned
        # installer and asks the person to install Ollama themselves instead.
        "installer_sha256": "",
        "installer_size_bytes": 0,
    },
    "recommended": "llama3.2:1b",
    "models": [
        {"id": "llama3.2:1b", "name": "Llama 3.2 1B", "size_bytes": 1_300_000_000,
         "min_ram_gb": 4, "note": "Fast, basic offline help."},
        {"id": "qwen2.5:3b", "name": "Qwen 2.5 3B", "size_bytes": 1_900_000_000,
         "min_ram_gb": 8, "note": "Better answers, needs more memory."},
    ],
}


def offline_catalog() -> dict:
    path = os.getenv("NOVA_OFFLINE_CATALOG", "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:  # a broken file must not take the endpoint down
            current_app.logger.warning("offline catalog %s unreadable: %s", path, e)
    return DEFAULT_OFFLINE_CATALOG


@bp.get("/v1/models/offline")
def get_offline_catalog():
    # Public: the desktop may need it before sign-in completes, and it holds
    # nothing about any person.
    return jsonify({"ok": True, "catalog": offline_catalog()})


__all__ = ["bp", "get_or_create_instance", "usage_today", "today", "ROLES"]
