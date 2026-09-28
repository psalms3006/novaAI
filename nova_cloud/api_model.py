"""nova_cloud.api_model — model access without handing out the key.

    POST /v1/model/live-token        short-lived Gemini Live token (voice)
    POST /gateway/<Generative Language path>   text requests, forwarded

Voice: the desktop opens its Live WebSocket straight to Google with an
ephemeral token minted here (locked to one model, valid for minutes), so audio
never passes through this server. Ephemeral tokens are documented for the Live
API only, so everything else -- planner, chat, tools, embeddings -- is
forwarded by the gateway with the server key. desk/creds.py already points
google-genai at `{cloud}/gateway` with the session in `X-NOVA-Session`.

Both paths check the account (via the normal access token), the plan's daily
limit, and the model allow-list before any key is used. Only counts are
recorded, never what was asked.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Callable

from flask import Blueprint, Response, current_app, g, jsonify, request, stream_with_context

from .auth_guard import user_required
from .db import session_scope
from .models import ModelUsage
from .api_instance import get_or_create_instance, today

log = logging.getLogger("nova.cloud.model")
bp = Blueprint("model", __name__)

GOOGLE_BASE = "https://generativelanguage.googleapis.com"
# The request shapes NOVA sends. Anything else (tuning, files, caching, model
# management) is refused rather than forwarded with the owner's key.
FORWARDABLE_METHODS = {"generateContent", "streamGenerateContent", "countTokens",
                       "embedContent", "batchEmbedContents"}
GATEWAY_MAX_BYTES = 20 * 1024 * 1024     # screenshots travel as inline images


# -- seams for tests -----------------------------------------------------------

def _default_genai_client(api_key: str):
    from google import genai
    return genai.Client(api_key=api_key, http_options={"api_version": "v1alpha"})


def _default_forward(method: str, url: str, headers: dict, body: bytes, stream: bool):
    import requests
    return requests.request(method, url, headers=headers, data=body, stream=stream,
                            timeout=(10, 300))


genai_client_factory: Callable = _default_genai_client
forward: Callable = _default_forward


# -- quota ---------------------------------------------------------------------

def _google_error(status: int, message: str, reason: str):
    """Errors in Google's own shape, so google-genai raises a readable
    ClientError in the desktop instead of a parse failure."""
    return jsonify({"error": {"code": status, "message": message, "status": reason}}), status


def _charge(kind: str):
    """Count one request against today's limit. Returns (instance_id, over)
    where `over` is (used, limit) when the limit is already reached."""
    cfg = current_app.config["NOVA_CFG"]
    with session_scope() as s:
        inst = get_or_create_instance(s, g.user_id)
        limit = int(cfg.plan_limits(inst.plan).get(kind, 0))
        row = s.get(ModelUsage, (inst.id, today(), kind))
        used = row.requests if row else 0
        if limit and used >= limit:
            return inst.id, (used, limit)
        if row is None:
            row = ModelUsage(instance_id=inst.id, day=today(), kind=kind, requests=0,
                             input_tokens=0, output_tokens=0)
            s.add(row)
        row.requests = used + 1
        return inst.id, None


def _add_tokens(instance_id: str, kind: str, usage: dict) -> None:
    try:
        with session_scope() as s:
            row = s.get(ModelUsage, (instance_id, today(), kind))
            if row is not None:
                row.input_tokens += int(usage.get("promptTokenCount", 0) or 0)
                row.output_tokens += int(usage.get("candidatesTokenCount", 0) or 0)
    except Exception as e:     # accounting must never break an answer
        log.warning("token accounting failed: %s", e)


# -- voice ---------------------------------------------------------------------

@bp.post("/v1/model/live-token")
@user_required
def live_token():
    cfg = current_app.config["NOVA_CFG"]
    if not cfg.gemini_api_key:
        return jsonify({"ok": False, "error": "model_unavailable",
                        "message": "NOVA's managed model is not configured on this server."}), 503
    _, over = _charge("live_token")
    if over:
        return jsonify({"ok": False, "error": "quota_exceeded",
                        "message": f"Today's voice limit is reached ({over[1]} sessions). "
                                   "It resets at midnight UTC."}), 429

    minutes = max(1, int(cfg.live_token_minutes))
    now = _dt.datetime.now(tz=_dt.timezone.utc)
    expires = now + _dt.timedelta(minutes=minutes)
    try:
        client = genai_client_factory(cfg.gemini_api_key)
        token = client.auth_tokens.create(config={
            # Enough uses for NOVA's session resumption and reconnects inside
            # the window; the window itself is the real bound.
            "uses": 20,
            "expire_time": expires,
            "new_session_expire_time": expires,
            # Only the model is locked; NOVA sets its own voice and tools.
            # (Field name verified against google-genai 2.17 and a real Live
            # session on 2026-09-28; an earlier name was rejected by the SDK.)
            "live_connect_constraints": {"model": cfg.live_model},
            "lock_additional_fields": [],
        })
    except Exception as e:
        log.error("live token mint failed: %s: %s", type(e).__name__, e)
        return jsonify({"ok": False, "error": "model_unavailable",
                        "message": "Could not start a voice session right now."}), 502
    return jsonify({"ok": True, "token": token.name, "model": cfg.live_model,
                    "expires_at": expires.isoformat(), "expires_in": minutes * 60,
                    "api_version": "v1alpha"})


# -- text gateway --------------------------------------------------------------

def _parse(path: str):
    """'v1beta/models/gemini-2.5-flash:generateContent' -> (version, model, method)."""
    parts = path.strip("/").split("/")
    if len(parts) != 3 or parts[1] != "models" or ":" not in parts[2]:
        return None
    model, method = parts[2].split(":", 1)
    if parts[0] not in ("v1", "v1beta", "v1alpha"):
        return None
    return parts[0], model, method


@bp.before_request
def _allow_larger_bodies():
    # The app-wide cap is 1 MB (metadata). Gateway requests carry screenshots.
    if request.path.startswith("/gateway/"):
        request.max_content_length = GATEWAY_MAX_BYTES


@bp.route("/gateway/<path:path>", methods=["POST"])
@user_required
def gateway(path: str):
    cfg = current_app.config["NOVA_CFG"]
    parsed = _parse(path)
    if parsed is None or parsed[2] not in FORWARDABLE_METHODS:
        return _google_error(404, "This request is not available through NOVA.", "NOT_FOUND")
    _version, model, method = parsed
    if not cfg.model_allowed(model):
        return _google_error(403, f"Model {model} is not available on your plan.",
                             "PERMISSION_DENIED")
    if not cfg.gemini_api_key:
        return _google_error(503, "NOVA's managed model is not configured on this server.",
                             "UNAVAILABLE")

    kind = "embed" if method in ("embedContent", "batchEmbedContents") else "generate"
    instance_id, over = _charge(kind)
    if over:
        return _google_error(429, f"Today's NOVA limit is reached ({over[1]} requests). "
                                  "It resets at midnight UTC.", "RESOURCE_EXHAUSTED")

    # Never forward what the client sent as credentials: the only key Google
    # sees is the server's.
    query = "&".join(f"{k}={v}" for k, v in request.args.items(multi=True)
                     if k.lower() != "key")
    url = f"{GOOGLE_BASE}/{path}" + (f"?{query}" if query else "")
    headers = {"Content-Type": "application/json", "x-goog-api-key": cfg.gemini_api_key}
    streaming = method == "streamGenerateContent"
    try:
        upstream = forward("POST", url, headers, request.get_data(), streaming)
    except Exception as e:
        log.error("gateway upstream error: %s", type(e).__name__)
        return _google_error(502, "The model service could not be reached.", "UNAVAILABLE")

    ctype = upstream.headers.get("Content-Type", "application/json")
    if streaming:
        def relay():
            try:
                for chunk in upstream.iter_content(chunk_size=None):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()
        return Response(stream_with_context(relay()), status=upstream.status_code,
                        content_type=ctype)

    body = upstream.content
    if upstream.status_code == 200 and kind == "generate":
        try:
            _add_tokens(instance_id, kind, (upstream.json() or {}).get("usageMetadata") or {})
        except Exception:
            pass
    return Response(body, status=upstream.status_code, content_type=ctype)


__all__ = ["bp", "FORWARDABLE_METHODS", "GATEWAY_MAX_BYTES"]
