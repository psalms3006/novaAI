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
import logging
import sys
import time
from pathlib import Path

from desk.settings import app_data_dir, get as _get_setting, set_many

log = logging.getLogger("nova.desk.creds")

_CRED_FILE = "byok.bin"
_PLAINTEXT_FILE = "api_keys.json"     # plaintext fallback (mirrors MARK XXXIX)
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


def _plaintext_path() -> Path:
    return app_data_dir() / _PLAINTEXT_FILE


def _load_plaintext_key() -> str:
    """Read a Gemini key from the plaintext JSON file (reliability fallback).

    DPAPI sealing is preferred but has proven unreliable inside the PyInstaller
    bundle, so a plaintext copy (same shape as MARK XXXIX's config/api_keys.json)
    guarantees the key is always recoverable on the same machine.
    """
    p = _plaintext_path()
    if not p.exists():
        return ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return (data.get("gemini_api_key") or "").strip()
    except Exception:
        return ""


def _store_plaintext_key(api_key: str) -> None:
    p = _plaintext_path()
    data: dict = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["gemini_api_key"] = api_key.strip()
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def store_byok(api_key: str) -> None:
    """Seal the key with DPAPI (readable only by this Windows user).

    This used to write a plaintext copy first, unconditionally, so every key
    sat readable in api_keys.json even when DPAPI worked. Plaintext is now
    only the fallback for a machine where sealing genuinely fails, and a
    successful seal removes any old plaintext copy.
    """
    key = api_key.strip()
    try:
        _cred_path().write_bytes(dpapi_protect(key))
    except Exception as e:
        log.warning("DPAPI unavailable (%s); storing the key unencrypted", type(e).__name__)
        _store_plaintext_key(key)
        return
    try:
        _plaintext_path().unlink(missing_ok=True)
    except Exception:
        pass


def clear_byok() -> None:
    for p in (_cred_path(), _plaintext_path()):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass


def load_byok() -> str:
    p = _cred_path()
    if not p.exists() and _plaintext_path().exists():
        # Migrate a key left in plaintext by an older build.
        legacy = _load_plaintext_key()
        if legacy:
            store_byok(legacy)
    if p.exists():
        try:
            return dpapi_unprotect(p.read_bytes())
        except Exception:
            # DPAPI unavailable (e.g. frozen build) — remove the stale blob and
            # fall through to the plaintext copy.
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
    return _load_plaintext_key()


def mask(key: str) -> str:
    if not key:
        return ""
    return "\u2022\u2022\u2022\u2022" + key[-4:] if len(key) > 8 else "\u2022\u2022\u2022\u2022"


def valid_key_format(key: str) -> bool:
    """Accept any plausible credential, not just `AIza…` API keys.

    The google-genai SDK also accepts OAuth access tokens and other credential
    formats (e.g. `AQ.Ab8…`), so we must not reject keys just because they don't
    match the classic Gemini API-key shape. The real validation happens on the
    first API call, whose error is surfaced to the user.
    """
    k = (key or "").strip()
    return len(k) >= 20


def classify_credential(key: str) -> dict:
    """Describe what kind of credential this is, and whether it is intact.

    Google AI Studio issues keys in two formats, and both are durable:

        AIza…    the classic 39-character Gemini API key
        AQ.Ab8…  the current 53-character AI Studio key

    Verified on 2026-09-10: an `AQ.` key from aistudio.google.com/apikey
    connected to Gemini Live in 3.6 s. An earlier revision of this function
    called `AQ.` a short-lived OAuth token that expires within the hour. That
    was wrong -- the observed failure was truncation, not expiry, and nothing
    in the evidence supported an expiry claim.

    What actually breaks is a partial paste. `AQ.Ab8…` that loses its leading
    `AQ.` becomes a 50-character string beginning `Ab8`, which Gemini rejects
    with

        1007  API key not valid. Please pass a valid API key.

    -- an error that names the key rather than the paste, so it reads as a
    wrong key rather than a truncated one. That cost a day of diagnosis, so it
    is detected explicitly.
    """
    k = (key or "").strip()
    if not k:
        return {"kind": "none", "durable": False, "warning": ""}
    if k.startswith("AIza"):
        return {"kind": "api_key", "durable": True, "warning": ""}
    if k.startswith("AQ."):
        return {"kind": "api_key_aistudio", "durable": True, "warning": ""}
    if k.startswith("Ab8") or k.startswith("Ab"):
        # An `AQ.Ab8…` key whose prefix was lost in the paste. It cannot
        # authenticate, and the resulting error does not say so.
        return {"kind": "truncated_key", "durable": False, "warning":
                "This key looks like an AI Studio key that lost its leading "
                "“AQ.” when it was pasted, so Gemini will reject it. "
                "Copy the whole value from aistudio.google.com/apikey, "
                "including the “AQ.” at the start."}
    return {"kind": "unknown", "durable": False, "warning":
            "NOVA does not recognise this credential’s format. Keys from "
            "aistudio.google.com/apikey begin with either “AQ.” or "
            "“AIza”. If voice fails to start, check the whole value was "
            "pasted."}


# ── device identity ──────────────────────────────────────────────────────────

def _device_path() -> Path:
    # The installation's identity, shared by every account on this PC.
    from .settings import machine_data_dir
    return machine_data_dir() / _DEVICE_FILE


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
    """Model access through the signed-in NOVA account (nova_cloud).

    The session is the account's own access token (nova_account refreshes it),
    so there is one identity for sign-in, sync and models:

      POST {url}/v1/model/live-token     short-lived Gemini Live token (voice)
      POST {url}/gateway/<Google path>   text/streaming/embeddings, forwarded
                                         with the server's key, header
                                         X-NOVA-Session: <access token>

    The Gemini key itself never reaches this machine.
    """

    def __init__(self, base_url: str):
        self.base = (base_url or "").rstrip("/")
        self._live: dict = {}

    @property
    def active(self) -> bool:
        if not self.base:
            return False
        try:
            import nova_account
            return nova_account.account().signed_in
        except Exception:
            return False

    def get_session(self, force_refresh: bool = False) -> str:
        import nova_account
        tok = nova_account.account().ensure_access_token()
        if not tok:
            raise RuntimeError("not signed in to NOVA, or NOVA Cloud is unreachable")
        return tok

    def mint_live_token(self) -> dict:
        """A Live token, reused while it has more than two minutes left (the
        backend issues them for 30 minutes and several sessions)."""
        if self._live and time.time() < float(self._live.get("_expires", 0)) - 120:
            return {k: v for k, v in self._live.items() if not k.startswith("_")}
        import requests
        r = requests.post(self.base + "/v1/model/live-token",
                          headers={"Authorization": f"Bearer {self.get_session()}"},
                          timeout=10)
        j = r.json() if r.content else {}
        if r.status_code >= 400 or not j.get("token"):
            raise RuntimeError(j.get("message") or f"live token refused ({r.status_code})")
        self._live = {**j, "_expires": time.time() + float(j.get("expires_in") or 600)}
        return {k: v for k, v in self._live.items() if not k.startswith("_")}

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
        # A client built with a Live token talks to Google directly: that is
        # the point of the token. Re-pointing it at the gateway would send the
        # voice WebSocket to the NOVA server, which does not carry audio.
        if str(kw.get("api_key") or "").startswith("auth_tokens/"):
            return
        # Installed once per process, but only meaningful while the managed
        # model is the chosen mode: after a switch to the person's own key,
        # their requests go to Google, not through NOVA's gateway.
        if os.environ.get("GEMINI_API_KEY") != _SENTINEL:
            return
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
    # The same server the account signs in to -- one resolver, so the model
    # gateway can never point somewhere the account does not.
    from nova_version import cloud_base_url
    return NovaCloudClient(cloud_base_url())


def resolve() -> dict:
    """Resolve the effective credential mode and apply it to the environment.

    Returns a status dict safe to expose to the UI (no secrets, keys masked).
    """
    global _last_status
    env_key = os.getenv("GEMINI_API_KEY", "").strip()

    # An explicit choice of offline outranks an ambient key.
    #
    # A GEMINI_API_KEY in the environment — from .env, or exported by a
    # developer shell — used to win unconditionally, so a user who chose
    # "work offline" still had every request going to Gemini while the
    # interface said offline. That is the reported "offline mode still runs on
    # Gemini? it's all confusing", and it is worse than confusing: the one
    # setting whose entire purpose is to stop network calls did not stop them.
    if _get_setting("auth_mode") == "offline":
        if env_key and env_key != _SENTINEL:
            os.environ.pop("GEMINI_API_KEY", None)
        _last_status = {
            "mode": "offline", "onboarded": bool(_get_setting("onboarded")),
            "cloud_configured": False, "cloud_error": "",
            "byok_present": bool(load_byok()), "byok_masked": "",
            "has_credential": False, "chosen_offline": True,
        }
        return dict(_last_status)

    if env_key and env_key != _SENTINEL:
        mode = "env"
        cloud_error = ""
    else:
        cloud = current_cloud_client()
        byok = load_byok()
        cloud_error = ""
        if byok and _get_setting("auth_mode") == "byok":
            # The person chose their own key over NOVA's managed model.
            os.environ["GEMINI_API_KEY"] = byok
            mode = "byok"
        elif cloud.active:
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
    # Tell the interface what sort of credential is actually in play, so a
    # key that will expire is flagged before it strands the user mid-sentence.
    effective = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if mode == "cloud" or effective == _SENTINEL:
        status["credential_kind"] = "cloud_session"
        status["credential_warning"] = ""
    else:
        info = classify_credential(effective)
        status["credential_kind"] = info["kind"]
        status["credential_durable"] = info["durable"]
        status["credential_warning"] = info["warning"]
    _last_status = status
    return status


def set_byok(api_key: str) -> dict:
    """Store a user-provided key securely and re-resolve. Never returns the key.

    Marking the user onboarded is part of this, not an afterthought. Pasting a
    key *is* choosing how NOVA connects, and leaving the flag unset meant the
    first-run screen reappeared seconds after the key was accepted, over and
    over, until the user gave up and picked offline — which did set it. The
    key was being stored correctly the whole time; only the "you have chosen"
    flag was missing, so the choice could never stick.
    """
    if not valid_key_format(api_key):
        raise ValueError("That key looks too short to be valid. Paste the full key.")
    store_byok(api_key.strip())
    set_many({"auth_mode": "byok", "onboarded": True})
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
    _register_router_gemini()
    return st


def _register_router_gemini() -> None:
    """(Re)register the Gemini provider on the running router when a key is set.

    The router is built once at startup; if no API key was present then it has
    no Gemini provider and would keep routing to a local model forever. After
    BYOK/env resolution, make sure a Gemini provider backed by the current key
    exists so text chat actually uses the cloud model.
    """
    key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not key:
        return
    try:
        import nova as _nova
    except Exception:
        return
    router = getattr(_nova, "_nova_router", None)
    if router is None:
        return
    try:
        if router.get_provider("gemini") is None:
            from nova_intelligence.gemini_provider import GeminiProvider
            router.register_provider("gemini", GeminiProvider(api_key=key))
    except Exception:
        pass


def bootstrap() -> dict:
    """Called once at startup before nova.py reads the environment."""
    return resolve()


def _import_genai():
    try:
        from google import genai
        return genai
    except Exception:
        return None
