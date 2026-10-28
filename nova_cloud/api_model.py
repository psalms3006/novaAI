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
import os
import threading
import time
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


# -- concurrency governance ----------------------------------------------------
#
# The process can already hold eight requests open at once (2 workers x 4
# threads), and each of those may keep an upstream call alive for up to five
# minutes. That ceiling was never chosen -- it is whatever the thread pool
# happened to allow -- so one client opening many streams can consume all of
# it, and every caller waits on the same per-key quota. These bounds are
# explicit, per process, and adjustable without a rebuild.
#
# They are deliberately counters behind a lock rather than one semaphore per
# user: a dict of semaphores would grow with the number of users who have ever
# been seen. An entry exists only while that user has a call in flight.

class _Saturated(Exception):
    """This process (or this account) is already at its concurrency ceiling."""

    status = 429

    def __init__(self, scope: str = "user"):
        super().__init__(
            "Too many requests at once for this account; retry shortly."
            if scope == "user" else
            "NOVA is handling as many requests as it can right now; retry shortly.")
        self.retry_after = 1


class _ProviderOpen(Exception):
    """The upstream is failing; calls are refused instead of queued."""

    status = 503

    def __init__(self, retry_after: int = 5):
        super().__init__("NOVA's model service is briefly unavailable; retry shortly.")
        self.retry_after = max(1, retry_after)


_GOV_LOCK = threading.Lock()
_GOV = {"active": 0, "rejected": 0, "failures": 0, "open_until": 0.0,
        "probing": False}
_USER_ACTIVE: dict[str, int] = {}


def _limit(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "").strip() or default))
    except ValueError:
        return default


def _global_limit() -> int:
    return _limit("NOVA_GATEWAY_GLOBAL_CONCURRENCY", 32)


def _user_limit() -> int:
    return _limit("NOVA_GATEWAY_USER_CONCURRENCY", 4)


def _breaker_threshold() -> int:
    return _limit("NOVA_GATEWAY_BREAKER_THRESHOLD", 5)


def _breaker_cooldown() -> int:
    return _limit("NOVA_GATEWAY_BREAKER_COOLDOWN", 30)


def activate(user_id: str) -> Callable[[], None]:
    """Take one concurrency slot, or raise telling the caller to retry.

    Returns an idempotent release callable. It has to be idempotent because a
    streamed response releases it twice: once when the relay generator ends and
    once from `call_on_close`, which also runs when the client disconnects
    before reading a single byte.
    """
    now = time.time()
    with _GOV_LOCK:
        open_until = _GOV["open_until"]
        if open_until:
            if now < open_until:
                raise _ProviderOpen(retry_after=int(open_until - now) + 1)
            # Half-open: exactly one probe may go through. A single failure
            # re-opens the breaker; a success closes it.
            if _GOV["probing"]:
                raise _ProviderOpen(retry_after=1)
            _GOV["probing"] = True
            _GOV["open_until"] = 0.0
            _GOV["failures"] = _breaker_threshold() - 1
        if _GOV["active"] >= _global_limit():
            _GOV["rejected"] += 1
            raise _Saturated("global")
        if _USER_ACTIVE.get(user_id, 0) >= _user_limit():
            _GOV["rejected"] += 1
            raise _Saturated("user")
        _GOV["active"] += 1
        _USER_ACTIVE[user_id] = _USER_ACTIVE.get(user_id, 0) + 1

    done = threading.Event()

    def release() -> None:
        if done.is_set():
            return
        done.set()
        with _GOV_LOCK:
            _GOV["active"] = max(0, _GOV["active"] - 1)
            n = _USER_ACTIVE.get(user_id, 1) - 1
            if n <= 0:
                _USER_ACTIVE.pop(user_id, None)    # idle users are forgotten
            else:
                _USER_ACTIVE[user_id] = n

    return release


def _note_success() -> None:
    with _GOV_LOCK:
        _GOV["failures"] = 0
        _GOV["open_until"] = 0.0
        _GOV["probing"] = False


def _note_failure() -> None:
    """Count one upstream failure, opening the breaker at the threshold.

    Google answering 429 counts: it means the shared per-key quota is already
    spent, and continuing to ask makes every caller slower.
    """
    with _GOV_LOCK:
        _GOV["probing"] = False
        _GOV["failures"] += 1
        if _GOV["failures"] >= _breaker_threshold():
            _GOV["open_until"] = time.time() + _breaker_cooldown()
            _GOV["failures"] = 0


def metrics_snapshot() -> dict:
    """Gateway numbers for /metrics. No user identifiers, ever."""
    now = time.time()
    with _GOV_LOCK:
        open_until = _GOV["open_until"]
        return {
            "active": _GOV["active"],
            "active_users": len(_USER_ACTIVE),
            "rejected_total": _GOV["rejected"],
            "consecutive_failures": _GOV["failures"],
            "breaker_open": bool(open_until and now < open_until),
            "breaker_retry_in_s": max(0, int(open_until - now)) if open_until else 0,
            "limits": {"global": _global_limit(), "per_user": _user_limit()},
        }


def reset_governor() -> None:
    """Test hook: forget all counters, as a fresh process would."""
    with _GOV_LOCK:
        _GOV.update(active=0, rejected=0, failures=0, open_until=0.0, probing=False)
        _USER_ACTIVE.clear()


# -- quota ---------------------------------------------------------------------

def _google_error(status: int, message: str, reason: str):
    """Errors in Google's own shape, so google-genai raises a readable
    ClientError in the desktop instead of a parse failure."""
    return jsonify({"error": {"code": status, "message": message, "status": reason}}), status


def _throttled(exc: Exception, reason: str):
    """A refusal with the standard shape plus a Retry-After the client honours."""
    resp, status = _google_error(exc.status, str(exc), reason)
    resp.headers["Retry-After"] = str(exc.retry_after)
    return resp, status


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
        release = activate(g.user_id)
    except (_Saturated, _ProviderOpen) as e:
        resp = jsonify({"ok": False, "error": "busy", "message": str(e)})
        resp.status_code = e.status
        resp.headers["Retry-After"] = str(e.retry_after)
        return resp
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
        _note_failure()
        log.error("live token mint failed: %s: %s", type(e).__name__, e)
        return jsonify({"ok": False, "error": "model_unavailable",
                        "message": "Could not start a voice session right now."}), 502
    finally:
        release()
    _note_success()
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
        release = activate(g.user_id)
    except _Saturated as e:
        return _throttled(e, "RESOURCE_EXHAUSTED")
    except _ProviderOpen as e:
        return _throttled(e, "UNAVAILABLE")

    try:
        upstream = forward("POST", url, headers, request.get_data(), streaming)
    except Exception as e:
        release()
        _note_failure()
        log.error("gateway upstream error: %s", type(e).__name__)
        return _google_error(502, "The model service could not be reached.", "UNAVAILABLE")

    if upstream.status_code >= 500 or upstream.status_code == 429:
        _note_failure()
    else:
        _note_success()

    ctype = upstream.headers.get("Content-Type", "application/json")
    if streaming:
        def relay():
            try:
                for chunk in upstream.iter_content(chunk_size=None):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()
                release()
        resp = Response(stream_with_context(relay()), status=upstream.status_code,
                        content_type=ctype)
        # A client that disconnects before reading a byte never runs relay().
        # call_on_close releases the slot there; release() is idempotent, so
        # the normal path releasing it twice is harmless.
        resp.call_on_close(release)
        return resp

    release()
    body = upstream.content
    if upstream.status_code == 200 and kind == "generate":
        try:
            _add_tokens(instance_id, kind, (upstream.json() or {}).get("usageMetadata") or {})
        except Exception:
            pass
    return Response(body, status=upstream.status_code, content_type=ctype)


__all__ = ["bp", "FORWARDABLE_METHODS", "GATEWAY_MAX_BYTES", "metrics_snapshot",
           "reset_governor"]
