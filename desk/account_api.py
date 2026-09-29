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

import os
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
        restarting = bind_signed_in_account(acct)
        _adopt_identity(acct, meta)
        pull_preferences_async()
        acct.emit("NOVA_STARTED", surface="desktop")
        return jsonify({"ok": True, "restarting": restarting, **_status_payload()})

    @app.post("/api/account/signin")
    @require_token
    def api_account_signin():
        body = request.get_json(silent=True) or {}
        acct = _acct()
        try:
            acct.sign_in(body.get("email", ""), body.get("password", ""))
        except nova_account.AccountError as e:
            return jsonify({"ok": False, "error": e.code, "message": e.message}), 400
        restarting = bind_signed_in_account(acct)
        _adopt_identity(acct, meta)
        pull_preferences_async()
        acct.emit("USER_LOGIN", surface="desktop")
        return jsonify({"ok": True, "restarting": restarting, **_status_payload()})

    @app.post("/api/account/signout")
    @require_token
    def api_account_signout():
        body = request.get_json(silent=True) or {}
        acct = _acct()
        acct.emit("USER_LOGOUT", surface="desktop")
        acct.flush(timeout=2.0)
        acct.sign_out(everywhere=bool(body.get("everywhere")))
        # The account's data stays in its own folder, untouched, for when they
        # sign back in. This process is still holding it -- memory loaded,
        # tasks running -- so NOVA restarts onto the sign-in screen rather
        # than let the next person inherit a live copy of someone else.
        import nova_lifecycle
        import nova_runtime
        restarting = bool(nova_lifecycle.active_account_id())
        nova_lifecycle.deactivate()
        if restarting and not _NO_RELAUNCH:
            nova_runtime.relaunch()
        return jsonify({"ok": True, "restarting": restarting, **_status_payload()})

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

    # -- first run / onboarding ---------------------------------------------
    #
    # What the window should show is decided here, from separate facts, never
    # one flag (nova_lifecycle): signed out -> sign in; unverified -> verify;
    # this account never set up -> profile; this account never set up on this
    # PC -> permissions/offline/startup; otherwise NOVA. Nothing here reads the
    # app version, so an update cannot bring any of it back.

    def _instance(acct) -> dict:
        inst = acct.cached_instance()
        if inst.get("pending_sync"):
            def _sync():
                try:
                    acct.complete_onboarding()
                except nova_account.AccountError:
                    pass
            threading.Thread(target=_sync, name="nova-onboarding-sync", daemon=True).start()
        if inst:
            return inst
        try:
            return acct.instance()
        except nova_account.AccountError:
            return {}

    def lifecycle_payload() -> dict:
        import nova_lifecycle
        import nova_runtime
        acct = _acct()
        first_run = os.environ.get("NOVA_FIRST_RUN") == "1"
        base = {"accounts_enabled": acct.configured, "first_run": first_run,
                "brain": nova_runtime.brain_state()}
        if not acct.configured:
            # Local build with no account server: the older local onboarding
            # (desk.creds) still applies; nothing to gate here.
            return {**base, "next": "none"}
        st = acct.status()
        if not st["signed_in"]:
            return {**base, "next": "sign_in", "signed_in": False,
                    "session_expired": bool(st.get("grace_expired"))}
        inst = _instance(acct)
        instance_done = bool(inst.get("onboarding_completed"))
        device_done = nova_lifecycle.device_setup_completed()
        if st["verification_required"] and not st["email_verified"]:
            nxt = "verify_email"
        elif not instance_done:
            nxt = "profile"
        elif not device_done:
            nxt = "device_setup"
        else:
            nxt = "none"
        return {**base, "next": nxt, "signed_in": True, "online": st["online"],
                "email_verified": st["email_verified"],
                "instance": inst, "instance_onboarding_completed": instance_done,
                "device_setup_completed": device_done}

    @app.get("/api/lifecycle")
    @require_token
    def api_lifecycle():
        return jsonify({"ok": True, **lifecycle_payload()})

    @app.post("/api/lifecycle/profile")
    @require_token
    def api_lifecycle_profile():
        body = request.get_json(silent=True) or {}
        fields = {k: str(body.get(k) or "").strip() for k in ("preferred_name", "role", "about")
                  if k in body}
        acct = _acct()
        try:
            acct.update_profile(**fields)
            # The profile is the account-level part of setup: once it is
            # answered (every field may be skipped), no device asks again.
            acct.complete_onboarding()
        except nova_account.Offline:
            return jsonify({"ok": False, "error": "offline",
                            "message": "Saving your profile needs a connection."}), 503
        except nova_account.AccountError as e:
            return jsonify({"ok": False, "error": e.code, "message": e.message}), 400
        local = {}
        if fields.get("preferred_name"):
            local["user_name"] = fields["preferred_name"]
            meta["user_name"] = fields["preferred_name"]
        if "role" in fields:
            local["user_occupation"] = fields["role"]
        if "about" in fields:
            local["user_about"] = fields["about"]
        if local:
            desk_settings.set_many(local)
        return jsonify({"ok": True, **lifecycle_payload()})

    @app.post("/api/lifecycle/complete")
    @require_token
    def api_lifecycle_complete():
        """Finish setup. Reports each step from what actually happened."""
        import nova_lifecycle
        body = request.get_json(silent=True) or {}
        steps = []
        acct = _acct()
        if not (acct.configured and acct.signed_in):
            return jsonify({"ok": False, "error": "not_signed_in"}), 400
        steps.append({"id": "account", "label": "Account connected", "ok": True})

        perms = body.get("permissions")
        if isinstance(perms, dict) and perms:
            desk_settings.set_many({"permissions": perms})
            steps.append({"id": "permissions", "label": "Permissions saved", "ok": True})

        mode = body.get("startup_mode")
        if mode:
            try:
                from . import startup
                r = startup.set_mode(mode)
                steps.append({"id": "startup", "ok": True,
                              "label": ("NOVA will start with Windows" if mode != "manual"
                                        else "NOVA will start when you open it")
                              + ("" if r.get("supported", True) else " (not supported here)")})
            except Exception as e:
                steps.append({"id": "startup", "ok": False, "label": "Startup preference",
                              "error": str(e)})

        offline = body.get("offline_model")
        if offline:
            steps.append({"id": "offline_model", "label": "Offline model",
                          "ok": offline == "ready",
                          "detail": {"ready": "verified and ready",
                                     "skipped": "skipped - you can download it later in Settings",
                                     "failed": "not downloaded - retry later in Settings"
                                     }.get(offline, offline)})

        nova_lifecycle.mark_device_setup_complete()
        steps.append({"id": "local", "label": "This PC set up", "ok": True})
        try:
            if not acct.cached_instance().get("onboarding_completed")                     or acct.cached_instance().get("pending_sync"):
                acct.complete_onboarding()
            steps.append({"id": "instance", "label": "Your NOVA is ready", "ok": True})
        except nova_account.AccountError:
            # Offline: the local part is done; the server is told next time.
            # Recorded as done here so the next launch does not ask again.
            acct._cache_instance({**acct.cached_instance(), "onboarding_completed": True,
                                  "pending_sync": True})
            steps.append({"id": "instance", "label": "Your NOVA is ready", "ok": True,
                          "detail": "will sync when you are online"})
        return jsonify({"ok": True, "steps": steps, **lifecycle_payload()})

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


# Tests exercise sign-out without ending the test process.
_NO_RELAUNCH = False


def bind_signed_in_account(acct: nova_account.NovaAccount) -> bool:
    """Point this process at the signed-in account's data and start NOVA.

    Returns True when a restart was needed instead: this process already
    holds a different person's NOVA (it should not happen through the UI,
    which only offers sign-in when nobody is signed in, but it must never
    mix two people's state if it does).
    """
    import nova_lifecycle
    import nova_runtime
    uid = acct.user_id
    if not uid:
        return False
    current = nova_lifecycle.active_account_id()
    if current and current != uid and nova_runtime.brain_state().get("started"):
        nova_lifecycle.activate_account(uid)
        if not _NO_RELAUNCH:
            nova_runtime.relaunch()
        return True
    nova_lifecycle.activate_account(uid)
    os.environ["NOVA_AUTH_GATE"] = "0"
    try:
        from . import creds
        creds.apply_runtime()                  # this account's key / managed model
    except Exception:
        pass
    nova_runtime.start_brain()
    return False


def _adopt_identity(acct: nova_account.NovaAccount, meta: dict) -> None:
    """Let NOVA use the account's name without asking the user again."""
    name = acct.display_name
    if name:
        desk_settings.set_many({"user_name": name})
        meta["user_name"] = name
