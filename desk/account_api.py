"""desk.account_api â€” the desktop's account surface.

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

import hmac
import logging
import os
import threading
import time

from flask import jsonify, request

import nova_account
from . import settings as desk_settings

log = logging.getLogger("nova.desk.account")

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


# ── Google sign-in handoff ────────────────────────────────────────────────────
#
# Google sends the browser back to the cloud (its registered redirect URI).
# The cloud cannot finish a desktop sign-in by itself: the session has to be
# adopted by the app, with this machine's device identity. So the flow is:
#
#   1. this process asks the cloud to start, passing its own loopback
#      callback; the cloud signs that address into the OAuth state;
#   2. Google returns the browser to the cloud, which redirects it to the
#      signed loopback address with the code;
#   3. the browser lands here, this process exchanges the code through the
#      cloud's POST callback, adopts the session, and the SPA's poll sees
#      `signed_in`.
#
# The loopback route cannot carry the desk token (a navigation sends no
# headers), so the state is its credential: one flow, ten minutes, one use.

_GOOGLE_FLOW_TTL_S = 600
_google_flow_lock = threading.Lock()
_google_flow: dict | None = None       # {"state": str, "issued_at": float}
_google_outcome: dict | None = None    # {"ok": bool, "message": str}


def _start_google_flow(state: str) -> None:
    global _google_flow, _google_outcome
    with _google_flow_lock:
        _google_flow = {"state": state, "issued_at": time.time()}
        _google_outcome = None


def _consume_google_flow(state: str) -> bool:
    """Accept the return trip once, and only for the flow this process began.

    A rejected attempt leaves the real flow alone: a stray request (a
    reloaded tab, a probe) must not be able to cancel the sign-in the
    person is in the middle of.
    """
    global _google_flow
    if not state:
        return False
    with _google_flow_lock:
        flow = _google_flow
        if not flow or time.time() - flow["issued_at"] > _GOOGLE_FLOW_TTL_S:
            return False
        if not hmac.compare_digest(str(flow.get("state") or ""), state):
            return False
        _google_flow = None
        return True


def _google_flow_pending() -> bool:
    with _google_flow_lock:
        return bool(_google_flow) and (
            time.time() - _google_flow["issued_at"] <= _GOOGLE_FLOW_TTL_S)


def _record_google_outcome(ok: bool, message: str) -> None:
    global _google_outcome
    with _google_flow_lock:
        _google_outcome = {"ok": ok, "message": message}


def _google_client_error() -> dict | None:
    """The failure the popup saw, for the SPA that cannot read the popup."""
    with _google_flow_lock:
        out = _google_outcome
        if not out or out.get("ok"):
            return None
        return {"message": out.get("message") or "Google sign-in failed."}


def _google_page(title: str, message: str, status: int,
                 autoclose: bool = False):
    """The last thing the person sees in the sign-in window."""
    from flask import Response
    from html import escape
    script = ("<script>setTimeout(function(){try{window.close()}catch(e){}},"
              "1200)</script>") if autoclose else ""
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)} - NOVA</title>
<style>
 body{{margin:0;min-height:100vh;display:grid;place-items:center;
      background:#0b0d12;color:#e8eaf0;
      font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}}
 main{{max-width:30rem;padding:2rem;text-align:center}}
 h1{{font-size:1.25rem;font-weight:600;margin:0 0 .6rem}}
 p{{margin:0;color:#a6adbb}}
</style></head>
<body><main><h1>{escape(title)}</h1><p>{escape(message)}</p></main>
{script}</body></html>"""
    return Response(body, status=status, mimetype="text/html",
                    headers={"Cache-Control": "no-store"})


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
        # The role comes from the backend's session payload; nothing here
        # grants one. (The previous in-flight patch had the client asking to
        # set its own OWNER role -- the backend refused it, and the attempt
        # is gone rather than left hoping the refusal stays.)
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
        # Silence first: if the relaunch does not happen (or takes a moment),
        # nobody should hear the previous account's NOVA finishing a sentence.
        nova_runtime.silence_voice()
        restarting = bool(nova_lifecycle.active_account_id())
        nova_lifecycle.deactivate()
        if restarting and not _NO_RELAUNCH:
            nova_runtime.relaunch()
        else:
            # Not restarting (no brain to unload, or a harness): at least
            # stop pointing at the folder we just left. Refuses while a
            # brain is live, which is exactly when a restart is required.
            nova_runtime.rebind_data_dir()
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

    @app.post("/api/account/email/verify_code")
    @require_token
    def api_account_verify_code():
        """Redeem the 6-digit code from the verification email.

        The UI offers this for people whose mail client will not open the
        link (or whose link expired). The code is checked by the cloud, which
        is the only place that may mark the account verified.
        """
        body = request.get_json(silent=True) or {}
        try:
            j = _acct().verify_code(body.get("code") or "")
            # The caller needs the refreshed account to know the gate opened:
            # the cloud's own reply carries only a human message.
            return jsonify({**j, "ok": True, **_status_payload()})
        except nova_account.Offline:
            return jsonify({"ok": False, "error": "offline",
                            "message": "Checking the code needs a connection."}), 503
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

    # -- Google OAuth -----------------------------------------------------------
    # These endpoints are called by the frontend during first-run setup.
    # They proxy to the nova_cloud backend, which holds the Google client
    # secret and performs the token exchange. This process receives the
    # session because the cloud sends the browser back to the loopback
    # address it signed into the state (see the flow notes above).

    def _adopt_google_session(data: dict) -> bool:
        """Make a Google sign-in this machine's session, exactly as a
        password sign-in would."""
        acct = _acct()
        # The session payload from the cloud, adopted without a second round
        # trip -- the cloud has already verified the account.
        acct._adopt(data)
        email = ((data.get("user") or {}).get("email") or "").strip()
        if email:
            # The device identity was created before we knew who they were;
            # move it onto the account so this machine is not a stranger to
            # its own next launch.
            nova_account.adopt_device_identity(email)
        restarting = bind_signed_in_account(acct)
        _adopt_identity(acct, meta)
        pull_preferences_async()
        acct.emit("USER_LOGIN", surface="desktop", method="google_oauth")
        return restarting

    @app.get("/api/account/google/start")
    @require_token
    def api_google_oauth_start():
        """Get the Google OAuth authorization URL."""
        try:
            import requests
            from urllib.parse import urlsplit
            from nova_version import cloud_base_url
            cloud = cloud_base_url()
            if not cloud:
                return jsonify({"ok": False, "error": "not_configured",
                                "message": "NOVA Cloud is not configured."}), 503
            base = request.host_url.rstrip("/")
            host = (urlsplit(base).hostname or "").lower()
            if host not in ("127.0.0.1", "localhost", "::1"):
                # Only the local window may finish a sign-in; anything else
                # would hand our session to whoever asked.
                return jsonify({"ok": False, "error": "not_supported",
                                "message": "Google sign-in needs the NOVA "
                                           "window. Sign in with email and "
                                           "password instead."}), 503
            callback = f"{base}/api/account/google/callback"
            resp = requests.get(f"{cloud}/v1/auth/google/start",
                                params={"client_redirect": callback}, timeout=10)
            if resp.status_code == 404:
                # The deployment predates this endpoint. Say that plainly
                # instead of surfacing "path → 404" to a person trying to sign in.
                return jsonify({"ok": False, "error": "not_supported",
                                "message": "Google sign-in is not available on this "
                                           "NOVA Cloud deployment yet. Use your email "
                                           "and password."}), 503
            try:
                data = resp.json()
            except ValueError:
                # A proxy/edge error page is not JSON; report it as the cloud
                # problem it is instead of raising a 500 inside the handler.
                return jsonify({"ok": False, "error": "bad_response",
                                "message": "NOVA Cloud returned an unexpected "
                                           "response. Try again shortly."}), 502
            if (resp.status_code >= 500
                    and data.get("error") == "oauth_not_configured"):
                # The deployment has the routes but no Google client. Its
                # message names environment variables; that is for the log,
                # not for the person trying to sign in.
                log.warning("[DESK] cloud cannot start Google sign-in: %s",
                            data.get("message") or "oauth_not_configured")
                return jsonify({
                    "ok": False, "error": "not_configured",
                    "message": "Google sign-in is not set up on NOVA Cloud "
                               "yet. Use your email and password for now.",
                }), 503
            if resp.status_code == 200 and data.get("ok") and data.get("state"):
                _start_google_flow(data["state"])
            return jsonify(data), resp.status_code
        except requests.RequestException as e:
            return jsonify({"ok": False, "error": "offline",
                            "message": f"NOVA Cloud unreachable: {e}"}), 503

    @app.get("/api/account/google/callback")
    def api_google_oauth_loopback():
        """The browser lands here after the cloud redirects Google's answer.

        This route cannot carry the desk token -- a browser navigation sends
        no headers -- so the one-time state from the flow this process
        started is the credential. Everything else is refused.
        """
        state = request.args.get("state") or ""
        if not _consume_google_flow(state):
            return _google_page(
                "Sign-in expired",
                "This sign-in is no longer valid. Close this window and try "
                "again from NOVA.", 400)

        error = request.args.get("error") or ""
        if error:
            msg = ("Sign-in was cancelled."
                   if error == "access_denied"
                   else "Google could not complete the sign-in.")
            _record_google_outcome(False, msg)
            return _google_page("Sign-in cancelled", msg, 400, autoclose=True)

        code = request.args.get("code") or ""
        if not code:
            _record_google_outcome(False, "Google did not return a code.")
            return _google_page(
                "Sign-in failed",
                "Google did not send a sign-in code. Close this window and "
                "try again from NOVA.", 400)

        try:
            import requests
            from nova_version import cloud_base_url
            cloud = cloud_base_url()
            if not cloud:
                _record_google_outcome(
                    False, "NOVA Cloud is not configured on this build.")
                return _google_page(
                    "Sign-in unavailable",
                    "This build is not connected to NOVA Cloud.", 503)
            resp = requests.post(
                f"{cloud}/v1/auth/google/callback",
                json={"code": code, "state": state,
                      **nova_account.device_descriptor()},
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            try:
                data = resp.json()
            except ValueError:
                data = {}
            if resp.status_code == 200 and data.get("ok"):
                _adopt_google_session(data)
                _record_google_outcome(True, "")
                return _google_page(
                    "Signed in",
                    "NOVA is signing you in. This window will close on its own.",
                    200, autoclose=True)
            msg = (data.get("message")
                   or "Google sign-in could not be completed.")
            _record_google_outcome(False, msg)
            status = 400 if 400 <= resp.status_code < 500 else 502
            return _google_page("Sign-in failed", msg, status)
        except requests.RequestException:
            msg = ("NOVA Cloud could not be reached. Check your connection "
                   "and try again.")
            _record_google_outcome(False, msg)
            return _google_page("Sign-in failed", msg, 503)

    @app.get("/api/account/google/status")
    @require_token
    def api_google_oauth_status():
        """What the window's own poll needs to know: still waiting, or why
        the popup it opened ended in failure."""
        return jsonify({"ok": True, "pending": _google_flow_pending(),
                        "error": _google_client_error()})

    @app.post("/api/account/google/callback")
    @require_token
    def api_google_oauth_callback():
        """Exchange a code the window obtained itself.

        The desktop loopback route above is the path a browser can follow;
        this remains for a caller that holds the code and the desk token.
        """
        body = request.get_json(silent=True) or {}
        try:
            import requests
            from nova_version import cloud_base_url
            cloud = cloud_base_url()
            if not cloud:
                return jsonify({"ok": False, "error": "not_configured",
                                "message": "NOVA Cloud is not configured."}), 503
            # Forward to the cloud backend
            resp = requests.post(
                f"{cloud}/v1/auth/google/callback",
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            try:
                data = resp.json()
            except ValueError:
                return jsonify({"ok": False, "error": "bad_response",
                                "message": "NOVA Cloud returned an unexpected "
                                           "response. Try again shortly."}), 502
            if resp.status_code == 200 and data.get("ok"):
                restarting = _adopt_google_session(data)
                return jsonify({"ok": True, "restarting": restarting,
                                **_status_payload()})
            return jsonify(data), resp.status_code
        except requests.RequestException as e:
            return jsonify({"ok": False, "error": "offline",
                            "message": f"NOVA Cloud unreachable: {e}"}), 503

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
        except nova_account.AccountError as e:
            # A deployment that requires verification refuses every authenticated
            # route, this one included. That refusal is the clearest statement
            # that this account has to verify first -- and unlike the session
            # flag it also works against a server older than this client, which
            # never sent the flag at all.
            if e.status in (401, 403) and "verify" in (e.message or "").lower():
                return {"verification_required": True}
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
        if ((st["verification_required"] or inst.get("verification_required"))
                and not st["email_verified"]):
            nxt = "verify_email"
        elif not instance_done:
            nxt = "profile"
        elif not device_done:
            nxt = "device_setup"
        else:
            nxt = "none"
            # Setup is genuinely finished and this is not a local-only run:
            # this is the one place that knows it. Ask only when the brain
            # is not already running (start_brain is guarded anyway; this
            # keeps repeat lifecycle polls from re-invoking it).
            if not nova_runtime.brain_state().get("started"):
                allowed, stage = nova_runtime.assistant_allowed()
                if allowed:
                    nova_runtime.start_brain()
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
        # Setup just finished: lifecycle_payload below discovers next=="none"
        # and starts the assistant (guarded, once). Nothing extra to call.
        return jsonify({"ok": True, "steps": steps,
                        **lifecycle_payload()})

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
    # This process started before anyone signed in, so nova -- imported when
    # the desk server came up -- fixed its memory paths to the machine-wide
    # folder. Without this, the brain started below reads the previous
    # person's living memory and greets the new account by the old name.
    # Repoint before the brain starts; if it cannot (a brain is already
    # running), restart rather than mix two people.
    if not nova_runtime.rebind_data_dir():
        if not _NO_RELAUNCH:
            nova_runtime.relaunch()
        return True
    try:
        from . import creds
        creds.apply_runtime()                  # this account's key / managed model
    except Exception:
        pass
    # The brain is the assistant: it listens, talks, runs tools. Starting it
    # here would give a half-onboarded account a live assistant behind the
    # setup screens, so the gate decides -- sign-in alone is not enough.
    # A fresh account has no cached instance; an empty cache reads as
    # "allowed", so fetch it now (the signup/sign-in request just proved the
    # backend is reachable) and fail closed when that is impossible.
    if not acct.cached_instance():
        try:
            acct.instance()
        except Exception:
            log.info("[DESK] instance unavailable at bind; deferring the "
                     "brain until the lifecycle check")
    # (The gate reads NOVA_AUTH_GATE, so it is consulted BEFORE this endpoint
    # touches the variable: clearing it first would disarm the check.)
    allowed, stage = nova_runtime.assistant_allowed()
    if os.environ.get("NOVA_DEBUG_BIND"):
        log.info("[BIND-DEBUG] cached=%r allowed=%r stage=%r",
                 acct.cached_instance(), allowed, stage)
    if allowed:
        os.environ["NOVA_AUTH_GATE"] = "0"
        nova_runtime.start_brain()
    else:
        os.environ["NOVA_AUTH_GATE"] = "1"
        # A session that was already talking when this account was bound must
        # not keep speaking behind the setup screens.
        nova_runtime.silence_voice()
        log.info("[DESK] brain deferred (%s): setup is not finished", stage)
    return False


def _adopt_identity(acct: nova_account.NovaAccount, meta: dict) -> None:
    """Let NOVA use the account's name without asking the user again."""
    name = acct.display_name
    if name:
        desk_settings.set_many({"user_name": name})
        meta["user_name"] = name
