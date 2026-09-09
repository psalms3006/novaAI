"""desk.account_api — the desktop's account surface.

Registered onto the desk Flask app. Everything here is deliberately thin: the
real logic lives in `nova_account`, and this module's job is to expose it to
the SPA and to keep the account off NOVA's conversational critical path.

Two rules the endpoints follow:

  * A cloud call never happens while answering the user. Sign-in and sign-out
    are user-initiated and may block; sync and telemetry never do.
  * When no backend is configured, every endpoint still answers, reporting
    `configured: false`. NOVA then runs entirely locally, which is a supported
    mode rather than a broken one -- the sign-in screen simply does not appear.
"""
from __future__ import annotations

import threading
import time

from flask import jsonify, request

import nova_account
from . import settings as desk_settings

# Preferences that live in the account rather than on the machine. Mirrors the
# backend's SYNCED_KEYS; a key absent from both stays device-local.
SYNCED_KEYS = {
    "theme", "accent", "density", "animations",
    "response_style", "history_turns", "show_tool_activity",
    "voice_responses", "continuous_conversation", "barge_in",
    "memory_enabled", "user_system_prompt",
    "telemetry_enabled", "cloud_sync_enabled",
}

_sync_lock = threading.Lock()
_last_pull = 0.0


def _acct() -> nova_account.NovaAccount:
    return nova_account.account()


def _local_synced_prefs() -> dict:
    """Local values for the synced keys, with their last-changed times."""
    allv = desk_settings.all()
    out = {}
    for k in SYNCED_KEYS:
        if k in allv:
            out[k] = {"value": allv[k], "updated_at": time.time()}
    return out


def pull_preferences_async() -> None:
    """Adopt the account's preferences in the background.

    Runs off the request thread so opening NOVA is never gated on the cloud.
    """
    def _work():
        global _last_pull
        acct = _acct()
        if not (acct.configured and acct.signed_in):
            return
        try:
            remote = acct.pull_preferences(since=0.0)
        except nova_account.AccountError:
            return                      # offline: keep whatever is local
        with _sync_lock:
            updates = {}
            for key, entry in remote.items():
                if key in SYNCED_KEYS:
                    updates[key] = entry.get("value")
            if updates:
                desk_settings.set_many(updates)
            _last_pull = time.time()

    threading.Thread(target=_work, name="nova-pref-pull", daemon=True).start()


def push_preferences_async(changed: dict) -> None:
    """Send local preference changes up, without making the UI wait."""
    payload = {k: {"value": v, "updated_at": time.time()}
               for k, v in (changed or {}).items() if k in SYNCED_KEYS}
    if not payload:
        return
    acct = _acct()
    if not (acct.configured and acct.signed_in):
        return

    def _work():
        try:
            acct.push_preferences(payload)
        except nova_account.AccountError:
            pass                        # offline: the local value already applied

    threading.Thread(target=_work, name="nova-pref-push", daemon=True).start()


def register(app, require_token, meta: dict) -> None:
    """Attach the account endpoints to the desk app."""

    def _status_payload() -> dict:
        acct = _acct()
        st = acct.status()
        st["display_name"] = acct.display_name
        return st

    @app.get("/api/account")
    @require_token
    def api_account_status():
        return jsonify(_status_payload())

    @app.post("/api/account/signup")
    @require_token
    def api_account_signup():
        body = request.get_json(silent=True) or {}
        acct = _acct()
        try:
            acct.sign_up(body.get("email", ""), body.get("password", ""),
                         body.get("display_name", ""))
        except nova_account.AccountError as e:
            return jsonify({"ok": False, "error": e.code, "message": e.message}), 400
        _adopt_identity(acct, meta)
        pull_preferences_async()
        acct.emit("NOVA_STARTED", surface="desktop")
        return jsonify({"ok": True, **_status_payload()})

    @app.post("/api/account/signin")
    @require_token
    def api_account_signin():
        body = request.get_json(silent=True) or {}
        acct = _acct()
        try:
            acct.sign_in(body.get("email", ""), body.get("password", ""))
        except nova_account.AccountError as e:
            return jsonify({"ok": False, "error": e.code, "message": e.message}), 400
        _adopt_identity(acct, meta)
        pull_preferences_async()
        acct.emit("USER_LOGIN", surface="desktop")
        return jsonify({"ok": True, **_status_payload()})

    @app.post("/api/account/signout")
    @require_token
    def api_account_signout():
        body = request.get_json(silent=True) or {}
        acct = _acct()
        acct.emit("USER_LOGOUT", surface="desktop")
        acct.flush(timeout=2.0)
        acct.sign_out(everywhere=bool(body.get("everywhere")))
        # The name NOVA calls the user by came from the account, so it goes
        # too. Nothing else local is touched: models, knowledge files and
        # conversations stay exactly where they are.
        desk_settings.set_many({"user_name": "User"})
        meta["user_name"] = "User"
        return jsonify({"ok": True, **_status_payload()})

    @app.post("/api/account/verify/resend")
    @require_token
    def api_account_resend_verification():
        try:
            return jsonify({"ok": True, **_acct().resend_verification()})
        except nova_account.Offline:
            return jsonify({"ok": False, "error": "offline",
                            "message": "Sending a new link needs a connection."}), 503
        except nova_account.AccountError as e:
            return jsonify({"ok": False, "error": e.code, "message": e.message}), 400

    @app.post("/api/account/refresh")
    @require_token
    def api_account_refresh_identity():
        """Re-read the account after the user follows a verification link."""
        try:
            st = _acct().refresh_identity()
            st["display_name"] = _acct().display_name
            return jsonify({"ok": True, **st})
        except nova_account.AccountError:
            return jsonify({"ok": False, **_status_payload()}), 503

    @app.get("/api/account/devices")
    @require_token
    def api_account_devices():
        try:
            return jsonify({"ok": True, "devices": _acct().devices()})
        except nova_account.Offline:
            return jsonify({"ok": False, "error": "offline",
                            "message": "Device list needs a connection."}), 503
        except nova_account.AccountError as e:
            return jsonify({"ok": False, "error": e.code, "message": e.message}), 400

    @app.post("/api/account/devices/<device_id>/revoke")
    @require_token
    def api_account_revoke(device_id: str):
        try:
            _acct().revoke_device(device_id)
            return jsonify({"ok": True})
        except nova_account.AccountError as e:
            return jsonify({"ok": False, "error": e.code, "message": e.message}), 400

    @app.patch("/api/account/devices/<device_id>")
    @require_token
    def api_account_rename(device_id: str):
        name = ((request.get_json(silent=True) or {}).get("name") or "").strip()
        try:
            _acct().rename_device(device_id, name)
            return jsonify({"ok": True})
        except nova_account.AccountError as e:
            return jsonify({"ok": False, "error": e.code, "message": e.message}), 400

    @app.post("/api/account/password/forgot")
    @require_token
    def api_account_forgot():
        body = request.get_json(silent=True) or {}
        acct = _acct()
        if not acct.configured:
            return jsonify({"ok": False, "error": "not_configured"}), 400
        try:
            out = acct._request("POST", "/v1/auth/password/forgot",
                                body={"email": body.get("email", "")})
            return jsonify({"ok": True, "message": out.get("message", "")})
        except nova_account.AccountError as e:
            return jsonify({"ok": False, "error": e.code, "message": e.message}), 400

    @app.post("/api/account/sync")
    @require_token
    def api_account_sync():
        """Push local synced preferences, then pull the account's view."""
        acct = _acct()
        if not (acct.configured and acct.signed_in):
            return jsonify({"ok": False, "error": "not_signed_in"}), 400
        try:
            result = acct.push_preferences(_local_synced_prefs())
        except nova_account.Offline:
            return jsonify({"ok": False, "error": "offline"}), 503
        pull_preferences_async()
        return jsonify({"ok": True, **result})


def _adopt_identity(acct: nova_account.NovaAccount, meta: dict) -> None:
    """Let NOVA use the account's name without asking the user again."""
    name = acct.display_name
    if name:
        desk_settings.set_many({"user_name": name})
        meta["user_name"] = name
