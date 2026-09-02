"""desk.creds — NOVA Desktop credential & authentication layer.

Architecture (production target):

    NOVA Desktop ──► NOVA auth backend ──► NOVA AI gateway ──► Gemini API
                          │
                          └── issues per-device identity + short-lived sessions

Credential resolution order (first match wins):

  1. "env"    — GEMINI_API_KEY already in the environment / .env (development,
                BYOK-via-env power users). Dev workflow is untouched.
  2. "cloud"  — NOVA_CLOUD_URL (env or the cloud_url setting) is configured and
                a device session can be established. All google-genai traffic is
                transparently redirected to the NOVA gateway via an SDK shim;
                the model sees a non-secret session placeholder, never a real
                Gemini key. The real credential stays server-side.
  3. "byok"   — user's own Gemini key, sealed with Windows DPAPI (user-scoped)
                in %APPDATA%\\NOVA\\byok.bin. Never written to disk in
                plaintext, never returned by any API (only a masked hint),
                never logged.
  4. "offline"— nothing available; the existing local/offline stack runs.

No provider is fabricated: cloud mode activates ONLY when an operator deploys a
backend and points clients at it via NOVA_CLOUD_URL. The REST contract the
client expects is documented on NovaCloudClient below.
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import secrets
import sys
import time
from pathlib import Path

from desk.settings import app_data_dir, get as _get_setting, set_many

_CRED_FILE = "byok.bin"
_DEVICE_FILE = "device.json"
_SENTINEL = "nova-cloud-session"      # truthy placeholder, NOT a secret
_LOG_REDACT = "nova-gateway-auth"     # what appears in logs instead of tokens


# ── DPAPI (Windows Data Protection API) ──────────────────────────────────────

_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def dpapi_protect(plain: str) -> bytes:
    """Seal a string so only the current Windows user can unseal it."""
    if sys.platform != "win32":
        raise OSError("DPAPI is Windows-only")
    out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(_blob(plain.encode("utf-8"))), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def dpapi_unprotect(blob: bytes) -> str:
    if sys.platform != "win32":
        raise OSError("DPAPI is Windows-only")
    out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(_blob(bytes(blob))), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise OSError("CryptUnprotectData failed (different user or corrupted)")
    try:
        return ctypes.string_at(out.pbData, out.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


# ── BYOK store ───────────────────────────────────────────────────────────────

def _cred_path() -> Path:
    return app_data_dir() / _CRED_FILE


def store_byok(api_key: str) -> None:
    _cred_path().write_bytes(dpapi_protect(api_key.strip()))


def clear_byok() -> None:
    try:
        _cred_path().unlink(missing_ok=True)
    except Exception:
        pass


def load_byok() -> str:
    p = _cred_path()
    if not p.exists():
        return ""
    try:
        return dpapi_unprotect(p.read_bytes())
    except Exception:
        # unreadable under this user account — treat as absent, remove it
        clear_byok()
        return ""


def mask(key: str) -> str:
    if not key:
        return ""
    return "\u2022\u2022\u2022\u2022" + key[-4:] if len(key) > 8 else "\u2022\u2022\u2022\u2022"


def valid_key_format(key: str) -> bool:
    k = (key or "").strip()
    return bool(k) and k.startswith("AIza") and 30 <= len(k) <= 60


# ── device identity ──────────────────────────────────────────────────────────

def _device_path() -> Path:
    return app_data_dir() / _DEVICE_FILE


def device_identity() -> dict:
    """Stable per-installation identity: {device_id, secret}.

    The secret is generated locally and stored DPAPI-sealed; it identifies the
    installation to the NOVA backend — it is not a Gemini credential.
    """
    p = _device_path()
    if p.exists():
        try:
            d = json.loads(dpapi_unprotect(p.read_bytes()))
            if d.get("device_id") and d.get("secret"):
                return d
        except Exception:
            pass
    ident = {
        "device_id": "nova-" + secrets.token_hex(8),
        "secret": secrets.token_urlsafe(32),
        "created": int(time.time()),
        "platform": sys.platform,
    }
    p.write_bytes(dpapi_protect(json.dumps(ident)))
    return ident


# ── NOVA cloud client (REST contract) ────────────────────────────────────────

class NovaCloudClient:
    """Client for the NOVA auth backend / AI gateway.

    Expected backend contract (implement server-side when deploying NOVA Cloud):

      POST {url}/v1/devices/register
           headers: X-NOVA-Device: <device_id>
           body:    {"secret": <device secret>, "platform": "windows"}
           200 ->   {"ok": true}

      POST {url}/v1/sessions
           headers: X-NOVA-Device: <device_id>, Authorization: Bearer <secret>
           200 ->   {"session_token": "...", "expires_in": 3600}

      POST {url}/v1/live/token          (optional; ephemeral Live credentials)
           headers: X-NOVA-Session: <session token>
           200 ->   {"token": "...", "expires_in": <seconds>}

      The gateway also accepts normal Generative-Language paths under
      {url}/gateway/... with header X-NOVA-Session instead of a Gemini key.

    Until such a backend exists, every method raises honestly and cloud mode
    stays inactive — nothing here pretends to succeed.
    """

    def __init__(self, base_url: str):
        self.base = (base_url or "").rstrip("/")
        self._session_token = ""
        self._expires = 0.0

    @property
    def active(self) -> bool:
        return bool(self.base)

    def _post(self, path: str, body: dict | None = None, headers: dict | None = None,
              timeout: float = 8.0) -> dict:
        import requests
        h = {"X-NOVA-Device": device_identity()["device_id"]}
        h.update(headers or {})
        r = requests.post(self.base + path, json=body or {}, headers=h, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def register_device(self) -> dict:
        ident = device_identity()
        return self._post("/v1/devices/register",
                          {"secret": ident["secret"], "platform": ident["platform"]})

    def get_session(self, force_refresh: bool = False) -> str:
        if self._session_token and not force_refresh and time.time() < self._expires - 60:
            return self._session_token
        ident = device_identity()
        j = self._post("/v1/sessions", {},
                       {"Authorization": f"Bearer {ident['secret']}"})
        tok = (j.get("session_token") or "").strip()
        if not tok:
            raise RuntimeError("cloud backend returned no session token")
        self._session_token = tok
        self._expires = time.time() + float(j.get("expires_in", 3600))
        return tok

    def mint_live_token(self) -> dict:
        return self._post("/v1/live/token", {},
                          {"X-NOVA-Session": self.get_session()})

    def gateway_base_url(self) -> str:
        return self.base + "/gateway"


# ── gateway shim: redirect ALL google-genai traffic to the NOVA gateway ──────

_shim_applied = False


def apply_gateway_shim(client) -> None:
    """Patch google.genai.Client so every request goes to the NOVA gateway.

    Called at most once per process, and only when cloud mode is active. The
    client's api_key argument becomes irrelevant (the gateway authenticates via
    the X-NOVA-Session header injected here); Google's endpoint is never hit.
    Sessions are refreshed lazily on each client construction — modules build
    short-lived clients per request, so tokens stay fresh automatically.
    """
    global _shim_applied
    if _shim_applied or client is None or not hasattr(client, "Client"):
        return
    cloud = current_cloud_client()
    if not cloud.active:
        return

    orig_init = client.Client.__init__

    def _patched_init(self, *a, **kw):
        orig_init(self, *a, **kw)
        try:
            session = cloud.get_session()          # cached; refreshes near expiry
            from google.genai import types as _t
            ho = _t.HttpOptions(base_url=cloud.gateway_base_url(),
                                headers={"X-NOVA-Session": session})
            self._api_client._http_options = ho        # noqa: SLF001
            self._async_api_client._http_options = ho  # noqa: SLF001
        except Exception:
            pass                                # construction still succeeds offline

    client.Client.__init__ = _patched_init
    _shim_applied = True


# ── resolution ───────────────────────────────────────────────────────────────

_last_status: dict = {}


def current_cloud_client() -> NovaCloudClient:
    url = (os.getenv("NOVA_CLOUD_URL") or _get_setting("cloud_url") or "").strip()
    return NovaCloudClient(url)


def resolve() -> dict:
    """Resolve the effective credential mode and apply it to the environment.

    Returns a status dict safe to expose to the UI (no secrets, keys masked).
    """
    global _last_status
    env_key = os.getenv("GEMINI_API_KEY", "").strip()

    if env_key and env_key != _SENTINEL:
        mode = "env"
        cloud_error = ""
    else:
        cloud = current_cloud_client()
        byok = load_byok()
        cloud_error = ""
        if cloud.active:
            try:
                cloud.get_session()
                apply_gateway_shim(_import_genai())
                os.environ["GEMINI_API_KEY"] = _SENTINEL
                mode = "cloud"
            except Exception as e:
                cloud_error = f"NOVA Cloud unreachable: {type(e).__name__}"
                mode = "byok" if byok else "offline"
                if byok:
                    os.environ["GEMINI_API_KEY"] = byok
        elif byok:
            os.environ["GEMINI_API_KEY"] = byok
            mode = "byok"
        else:
            mode = "offline"

    status = {
        "mode": mode,
        "onboarded": bool(_get_setting("onboarded")),
        "cloud_configured": bool(current_cloud_client().active),
        "cloud_error": cloud_error,
        "byok_present": bool(load_byok()),
        "byok_masked": mask(load_byok()) if mode == "byok" else "",
        "has_credential": mode in ("env", "cloud", "byok"),
    }
    _last_status = status
    return status


def set_byok(api_key: str) -> dict:
    """Store a user-provided key securely and re-resolve. Never returns the key."""
    if not valid_key_format(api_key):
        raise ValueError("That doesn't look like a Gemini API key (expected AIza…).")
    store_byok(api_key.strip())
    set_many({"auth_mode": "byok"})
    return apply_runtime()


def clear_byok_and_apply() -> dict:
    clear_byok()
    if os.environ.get("GEMINI_API_KEY") and os.environ.get("GEMINI_API_KEY") != _SENTINEL \
            and _last_status.get("mode") == "byok":
        os.environ.pop("GEMINI_API_KEY", None)
    return apply_runtime()


def choose_offline() -> dict:
    set_many({"auth_mode": "offline", "onboarded": True})
    return apply_runtime()


def choose_cloud(url: str = "") -> dict:
    if url:
        set_many({"cloud_url": url.strip()})
    st = apply_runtime()
    if st["mode"] != "cloud":
        raise RuntimeError(st.get("cloud_error") or "NOVA Cloud is not reachable.")
    set_many({"auth_mode": "cloud", "onboarded": True})
    return apply_runtime()


def mark_onboarded() -> None:
    set_many({"onboarded": True})


def apply_runtime() -> dict:
    """Re-resolve now and push the result into already-imported modules."""
    st = resolve()
    try:
        import nova as _nova
        _nova.GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
    except Exception:
        pass
    try:
        from desk import chat as _chat
        _chat.GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
    except Exception:
        pass
    return st


def bootstrap() -> dict:
    """Called once at startup before nova.py reads the environment."""
    return resolve()


def _import_genai():
    try:
        from google import genai
        return genai
    except Exception:
        return None
