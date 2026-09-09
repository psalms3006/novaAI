"""nova_account — the NOVA desktop's account client.

Three properties this module exists to guarantee:

  1. **Nothing here is on the conversation's critical path.** Sync and
     telemetry run on a background thread. `ensure_access_token()` is the only
     call that can touch the network, and it does so only when the cached token
     has actually expired.
  2. **Losing the network does not log the user out.** After a successful
     authentication the device keeps an offline grant: the account, the
     profile and the last synced preferences stay usable until the grace
     period expires. NOVA's local capabilities are unaffected by the backend
     being unreachable.
  3. **Signing out removes the account, not the machine.** Models, ZIM files,
     maps and the installation itself are untouched -- only the credentials and
     that account's cached identity are cleared.

Multiple accounts on one computer are supported: each account gets its own
device identity on the installation, so one person's cached profile and
preferences can never be shown to another, and revoking one person's laptop
does not sign the other out.
"""
from __future__ import annotations

import platform
import queue
import socket
import threading
import time
import uuid
from typing import Callable

import nova_secure_store as store

APP_VERSION = "0.1.0"

# Secure-store keys.
_K_SESSION = "account_session"

_DEFAULT_TIMEOUT = 8.0


# -- device identity ---------------------------------------------------------
#
# A "device" is one installation belonging to one account, not one computer.
# Two people sharing a machine each get their own device identity, so one
# person revoking their laptop cannot sign the other out, and neither account
# can be reached through the other's registration. That is also what keeps
# their local caches separate.

_K_INSTALL = "installation_identity"
_K_DEVICES = "device_identities"          # account key -> {device_id, secret}


def installation_identity() -> dict:
    """Stable per-installation values, shared by every account on this machine.

    Deliberately not derived from MAC address, hostname, disk serial or OS
    username: all of those change (new network card, renamed machine, cloned
    VM) and none of them is a secret.
    """
    ident = store.get_json(_K_INSTALL)
    if isinstance(ident, dict) and ident.get("installation_id"):
        return ident
    ident = {"installation_id": str(uuid.uuid4()), "created": time.time()}
    store.set_json(_K_INSTALL, ident)
    return ident


def _account_key(email: str) -> str:
    return (email or "").strip().lower()


def device_identity(email: str = "") -> dict:
    """The device identity this account uses on this installation."""
    key = _account_key(email)
    table = store.get_json(_K_DEVICES) or {}
    entry = table.get(key)
    if isinstance(entry, dict) and entry.get("device_id") and entry.get("secret"):
        return entry
    entry = {
        "device_id": str(uuid.uuid4()),
        "secret": uuid.uuid4().hex + uuid.uuid4().hex,
        "created": time.time(),
    }
    table[key] = entry
    store.set_json(_K_DEVICES, table)
    return entry


def forget_device_identity(email: str) -> None:
    """Drop this account's device identity from the machine."""
    table = store.get_json(_K_DEVICES) or {}
    if table.pop(_account_key(email), None) is not None:
        store.set_json(_K_DEVICES, table)


def device_descriptor(email: str = "") -> dict:
    """What this device tells the backend about itself."""
    ident = device_identity(email)
    try:
        name = socket.gethostname() or "NOVA device"
    except Exception:
        name = "NOVA device"
    return {
        "device_id": ident["device_id"],
        "device_secret": ident["secret"],
        "platform": platform.system().lower(),      # windows / darwin / linux
        "app_version": APP_VERSION,
        "device_name": name[:120],
    }


# -- errors ------------------------------------------------------------------

class AccountError(Exception):
    """A request the backend refused, with a message meant for the user."""

    def __init__(self, message: str, code: str = "error", status: int = 0):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


class Offline(AccountError):
    """The backend could not be reached. Never fatal on its own."""

    def __init__(self, message: str = "NOVA Cloud is unreachable."):
        super().__init__(message, "offline", 0)


# -- the client --------------------------------------------------------------

class NovaAccount:
    """Owns the signed-in state for one NOVA installation."""

    def __init__(self, base_url: str = "", on_change: Callable[[], None] | None = None):
        import os
        self.base = (base_url or os.getenv("NOVA_CLOUD_URL", "")).rstrip("/")
        self._lock = threading.RLock()
        self._on_change = on_change
        self._session: dict = store.get_json(_K_SESSION) or {}
        self._last_online: float = float(self._session.get("last_verified", 0) or 0)
        self._online = False

        self._tel_q: queue.Queue = queue.Queue(maxsize=500)
        self._tel_thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- state -------------------------------------------------------------

    @property
    def configured(self) -> bool:
        """False when no backend is deployed. NOVA then runs purely locally,
        which is a supported mode, not an error."""
        return bool(self.base)

    @property
    def signed_in(self) -> bool:
        with self._lock:
            if not self._session.get("user"):
                return False
            return not self._grace_expired()

    @property
    def user(self) -> dict:
        with self._lock:
            return dict(self._session.get("user") or {})

    @property
    def user_id(self) -> str:
        return self.user.get("id", "")

    @property
    def display_name(self) -> str:
        """What NOVA should call the user, without asking them again."""
        u = self.user
        return (u.get("display_name") or u.get("email", "").split("@")[0]
                or "").strip()

    @property
    def online(self) -> bool:
        return self._online

    def _email(self) -> str:
        return (self.user.get("email") or "").strip().lower()

    def _grace_expired(self) -> bool:
        grace = float(self._session.get("offline_grace_s") or 30 * 86400)
        last = float(self._session.get("last_verified") or 0)
        return bool(last) and (time.time() - last) > grace

    def status(self) -> dict:
        """Everything a UI needs, with no network access."""
        with self._lock:
            sess = dict(self._session)
        return {
            "configured": self.configured,
            "signed_in": self.signed_in,
            "online": self._online,
            "user": sess.get("user") or None,
            "device_id": (sess.get("device") or {}).get("id")
                         or device_identity(self._email())["device_id"],
            "credential_backend": store.backend(),
            "hardware_backed": store.is_hardware_backed(),
            "last_verified": sess.get("last_verified"),
            "offline_grace_s": sess.get("offline_grace_s"),
            "grace_expired": self._grace_expired() if sess.get("user") else False,
        }

    # -- HTTP --------------------------------------------------------------

    def _request(self, method: str, path: str, *, body: dict | None = None,
                 token: str | None = None, timeout: float = _DEFAULT_TIMEOUT) -> dict:
        if not self.configured:
            raise AccountError("NOVA Cloud is not configured on this build.",
                               "not_configured")
        import requests
        headers = {"Content-Type": "application/json",
                   "X-NOVA-Client": f"nova-desktop/{APP_VERSION}"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            r = requests.request(method, self.base + path, json=body,
                                 headers=headers, timeout=timeout)
        except Exception as e:
            self._online = False
            raise Offline(f"NOVA Cloud is unreachable ({type(e).__name__}).")
        self._online = True
        try:
            payload = r.json()
        except Exception:
            payload = {}
        if r.status_code >= 400:
            raise AccountError(
                payload.get("message") or f"Request failed ({r.status_code}).",
                payload.get("error") or "error", r.status_code)
        return payload

    # -- auth --------------------------------------------------------------

    def sign_up(self, email: str, password: str, display_name: str = "") -> dict:
        payload = self._request("POST", "/v1/auth/signup", body={
            "email": email, "password": password, "display_name": display_name,
            **device_descriptor(email)})
        self._adopt(payload)
        return self.status()

    def sign_in(self, email: str, password: str) -> dict:
        payload = self._request("POST", "/v1/auth/login", body={
            "email": email, "password": password, **device_descriptor(email)})
        self._adopt(payload)
        return self.status()

    def _adopt(self, payload: dict) -> None:
        with self._lock:
            self._session = {
                "user": payload.get("user") or {},
                "device": payload.get("device") or {},
                "access_token": payload.get("access_token") or "",
                "access_expires": time.time() + float(payload.get("expires_in") or 900),
                "refresh_token": payload.get("refresh_token") or "",
                "offline_grace_s": float(payload.get("offline_grace_s") or 30 * 86400),
                "last_verified": time.time(),
            }
            store.set_json(_K_SESSION, self._session)
        self._last_online = time.time()
        self._notify()
        self.start_background()

    def sign_out(self, everywhere: bool = False) -> dict:
        """Clear the account from this device.

        Local resources are explicitly untouched: models, knowledge files and
        the installation are not the account's to delete.
        """
        with self._lock:
            refresh = self._session.get("refresh_token") or ""
            token = self._session.get("access_token") or ""
        try:
            if everywhere and token:
                self._request("POST", "/v1/auth/logout_all", token=token)
            elif refresh:
                self._request("POST", "/v1/auth/logout", body={"refresh_token": refresh})
        except AccountError:
            # An unreachable backend must not trap the user in a signed-in
            # state. The local credentials go regardless.
            pass
        with self._lock:
            self._session = {}
            store.delete_secret(_K_SESSION)
        # The device identity stays: signing back in on this machine should
        # reuse the same registration rather than accumulating a new device
        # row on every sign-out. Local models, knowledge files and the
        # installation are untouched.
        self._notify()
        return self.status()

    def ensure_access_token(self, *, allow_network: bool = True) -> str:
        """A usable access token, refreshed only when actually needed."""
        with self._lock:
            tok = self._session.get("access_token") or ""
            exp = float(self._session.get("access_expires") or 0)
            refresh = self._session.get("refresh_token") or ""
        if tok and time.time() < exp - 60:
            return tok
        if not refresh or not allow_network:
            return ""
        try:
            payload = self._request("POST", "/v1/auth/refresh",
                                    body={"refresh_token": refresh})
        except Offline:
            # Offline is not a sign-out. Keep the cached identity and let the
            # grace period decide.
            return ""
        except AccountError as e:
            if e.status in (401, 403):
                # The backend actively rejected this session -- revoked device,
                # disabled account, or a leaked token. That is a real sign-out.
                with self._lock:
                    self._session = {}
                    store.delete_secret(_K_SESSION)
                self._notify()
            return ""
        with self._lock:
            self._session["access_token"] = payload.get("access_token") or ""
            self._session["access_expires"] = time.time() + float(
                payload.get("expires_in") or 900)
            self._session["refresh_token"] = payload.get("refresh_token") or refresh
            self._session["last_verified"] = time.time()
            store.set_json(_K_SESSION, self._session)
            return self._session["access_token"]

    # -- devices -----------------------------------------------------------

    def devices(self) -> list[dict]:
        tok = self.ensure_access_token()
        if not tok:
            raise Offline()
        return self._request("GET", "/v1/devices", token=tok).get("devices", [])

    def rename_device(self, device_id: str, name: str) -> dict:
        tok = self.ensure_access_token()
        return self._request("PATCH", f"/v1/devices/{device_id}",
                             body={"name": name}, token=tok)

    def revoke_device(self, device_id: str) -> dict:
        tok = self.ensure_access_token()
        return self._request("POST", f"/v1/devices/{device_id}/revoke", token=tok)

    # -- preferences -------------------------------------------------------

    def pull_preferences(self, since: float = 0.0) -> dict:
        tok = self.ensure_access_token()
        if not tok:
            raise Offline()
        j = self._request("GET", f"/v1/sync/preferences?since={since}", token=tok)
        return j.get("preferences", {})

    def push_preferences(self, prefs: dict) -> dict:
        tok = self.ensure_access_token()
        if not tok:
            raise Offline()
        return self._request("POST", "/v1/sync/preferences",
                             body={"preferences": prefs}, token=tok)

    def flags(self) -> dict:
        tok = self.ensure_access_token()
        if not tok:
            raise Offline()
        return self._request("GET", "/v1/sync/flags", token=tok).get("flags", {})

    # -- telemetry (always non-blocking) -----------------------------------

    def emit(self, event_type: str, **attrs) -> None:
        """Queue one operational event. Never blocks, never raises.

        If the queue is full the event is dropped: telemetry is the least
        important thing NOVA is doing at any moment.
        """
        try:
            self._tel_q.put_nowait({"type": event_type, "attrs": attrs,
                                    "ts": time.time()})
        except queue.Full:
            pass

    def emit_model_call(self, provider: str, model: str, **kw) -> None:
        try:
            self._tel_q.put_nowait({"kind": "model_call", "provider": provider,
                                    "model": model, **kw})
        except queue.Full:
            pass

    def emit_agent_run(self, agent: str, **kw) -> None:
        try:
            self._tel_q.put_nowait({"kind": "agent_run", "agent": agent, **kw})
        except queue.Full:
            pass

    def emit_error(self, code: str, **context) -> None:
        try:
            self._tel_q.put_nowait({"kind": "error", "code": code,
                                    "app_version": APP_VERSION,
                                    "platform": platform.system().lower(),
                                    "context": context})
        except queue.Full:
            pass

    def start_background(self) -> None:
        if self._tel_thread is not None or not self.configured:
            return
        self._stop.clear()
        self._tel_thread = threading.Thread(target=self._background,
                                            name="nova-account-sync", daemon=True)
        self._tel_thread.start()

    def stop_background(self) -> None:
        self._stop.set()
        self._tel_thread = None

    def _background(self) -> None:
        """Batch telemetry and refresh the session, entirely off the hot path."""
        batch: list[dict] = []
        last_flush = time.time()
        while not self._stop.is_set():
            try:
                item = self._tel_q.get(timeout=1.0)
                batch.append(item)
            except queue.Empty:
                pass
            due = (len(batch) >= 50) or (batch and time.time() - last_flush > 15)
            if not due:
                continue
            last_flush = time.time()
            payload, batch = batch, []
            if not self.signed_in:
                continue
            try:
                tok = self.ensure_access_token()
                if tok:
                    self._request("POST", "/v1/telemetry/events",
                                  body={"events": payload}, token=tok, timeout=5.0)
            except Exception:
                # Telemetry loss is acceptable; blocking or crashing is not.
                pass

    def _notify(self) -> None:
        if self._on_change:
            try:
                self._on_change()
            except Exception:
                pass


# -- module-level singleton --------------------------------------------------

_account: NovaAccount | None = None
_account_lock = threading.Lock()


def account() -> NovaAccount:
    global _account
    with _account_lock:
        if _account is None:
            _account = NovaAccount()
        return _account


__all__ = ["NovaAccount", "AccountError", "Offline", "account",
           "device_identity", "device_descriptor", "APP_VERSION"]
