"""nova_cloud.api_capabilities — what NOVA installations know how to reach.

    GET  /v1/capabilities/catalog     signed-in device: enabled providers
    POST /v1/capabilities/report      signed-in device: {provider_id, passed}

The catalog is global knowledge (§ "global knowledge vs user credentials"):
how a provider is reached, what it costs, what it needs. It never holds a
credential, and `validate_entry` refuses a setup block that looks like one, so
a mistake by the owner cannot publish a key to every installation.

Reports are deliberately narrow: an installation may say only that a *listed*
provider passed or failed its own local test. It cannot add providers, and it
cannot send what it did with them -- the catalog cannot be poisoned by a
client, and nothing about the person's use leaves their machine.
"""
from __future__ import annotations

import re

from flask import Blueprint, g, jsonify, request
from sqlalchemy import select

from .auth_guard import user_required
from .db import RateLimited, rate_limit, session_scope
from .models import CatalogProvider

bp = Blueprint("capabilities", __name__, url_prefix="/v1/capabilities")

KINDS = ("http_api", "mcp_server", "extension", "builtin_tool")
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_SECRETISH = re.compile(r"(api[_-]?key|secret|password|token|bearer|private)", re.I)
_SECRET_VALUE = re.compile(r"^(sk|pk|rk|ghp|gho|xox[abp]|AIza)[-_A-Za-z0-9]{12,}$")
#: Setup keys that name *where* a credential goes, not the credential itself.
_ALLOWED_SETUP_KEYS = {"auth_header", "auth_scheme"}


def validate_entry(entry: dict) -> dict:
    """Normalise a catalog entry, or raise ValueError saying what is wrong."""
    pid = str(entry.get("id") or "").strip().lower()
    if not _ID.match(pid):
        raise ValueError("id must be lowercase letters, digits and dashes")
    name = str(entry.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    kind = str(entry.get("kind") or "http_api")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    setup = entry.get("setup") or {}
    if not isinstance(setup, dict):
        raise ValueError("setup must be an object")
    for k, v in setup.items():
        if k not in _ALLOWED_SETUP_KEYS and _SECRETISH.search(str(k)):
            raise ValueError(f"setup.{k} looks like a credential; the catalog never holds one")
        if isinstance(v, str) and _SECRET_VALUE.match(v.strip()):
            raise ValueError(f"setup.{k} looks like a credential; the catalog never holds one")
    base = str(setup.get("base_url") or "")
    if kind == "http_api" and not base.startswith("https://"):
        raise ValueError("an http_api provider needs an https:// setup.base_url")
    tags = entry.get("tags") or entry.get("capabilities") or []
    return {"id": pid, "name": name[:120], "description": str(entry.get("description") or "")[:2000],
            "kind": kind, "cost": str(entry.get("cost") or "")[:64],
            "requires_account": bool(entry.get("requires_account")),
            "data_leaves_device": bool(entry.get("data_leaves_device", kind == "http_api")),
            "docs_url": str(entry.get("docs_url") or "")[:500], "setup": setup,
            "tags": [str(t)[:40] for t in tags][:20]}


def upsert(entry: dict) -> dict:
    """Add or update a catalog entry. Used by manage.py."""
    e = validate_entry(entry)
    with session_scope() as s:
        row = s.get(CatalogProvider, e["id"])
        if row is None:
            row = CatalogProvider(id=e["id"])
            s.add(row)
        for k, v in e.items():
            setattr(row, k, v)
        row.enabled = bool(entry.get("enabled", True))
    return e


def _public(row: CatalogProvider) -> dict:
    return {"id": row.id, "name": row.name, "description": row.description, "kind": row.kind,
            "cost": row.cost, "requires_account": row.requires_account,
            "data_leaves_device": row.data_leaves_device, "docs_url": row.docs_url,
            "setup": row.setup or {}, "capabilities": row.tags or [],
            "validated_count": row.validated_count, "failed_count": row.failed_count}


@bp.get("/catalog")
@user_required
def catalog():
    with session_scope() as s:
        rows = s.scalars(select(CatalogProvider).where(CatalogProvider.enabled.is_(True))
                         .order_by(CatalogProvider.validated_count.desc())).all()
        return jsonify({"ok": True, "providers": [_public(r) for r in rows]})


@bp.post("/report")
@user_required
def report():
    body = request.get_json(silent=True) or {}
    pid = str(body.get("provider_id") or "").strip().lower()
    if not _ID.match(pid) or not isinstance(body.get("passed"), bool):
        return jsonify({"ok": False, "error": "bad_report"}), 400
    with session_scope() as s:
        try:
            rate_limit(s, f"capreport:{g.user_id}:{pid}", limit=5, window_s=86400)
        except RateLimited as e:
            return jsonify({"ok": False, "error": "rate_limited"}), 429, \
                {"Retry-After": str(e.retry_after)}
        row = s.get(CatalogProvider, pid)
        if row is None or not row.enabled:
            return jsonify({"ok": False, "error": "unknown_provider"}), 404
        if body["passed"]:
            row.validated_count += 1
        else:
            row.failed_count += 1
    return jsonify({"ok": True})


__all__ = ["bp", "validate_entry", "upsert"]
