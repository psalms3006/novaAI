"""nova_secure_store — cross-platform storage for NOVA's own secrets.

NOVA's session tokens must not sit in a plain file next to the settings. This
module puts them in whatever the operating system provides:

    Windows   Credential Manager   (via keyring)
    macOS     Keychain             (via keyring)
    Linux     Secret Service       (via keyring, if a keyring daemon is running)

Where none of those is available -- a headless Linux box, a locked-down
container -- there is a file fallback under the user's own profile directory,
encrypted with a key derived from a machine-local secret and written with
owner-only permissions. That is weaker than an OS keystore and says so: callers
can ask `backend()` and surface it, rather than being told everything is fine.

The existing `desk.creds` DPAPI path stays as-is for the Gemini BYOK key on
Windows. This module is deliberately separate, because account tokens and model
provider credentials are different security domains and should not share a
store.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import sys
import threading
from pathlib import Path

SERVICE = "NOVA"
_lock = threading.RLock()
_keyring = None
_keyring_checked = False


def _try_keyring():
    """Import keyring and confirm the backend actually works.

    An importable keyring is not a working keyring: on Linux without a secret
    service the backend raises only when used. So we probe with a real
    round trip before trusting it.
    """
    global _keyring, _keyring_checked
    if _keyring_checked:
        return _keyring
    _keyring_checked = True
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring
        kr = keyring.get_keyring()
        if isinstance(kr, FailKeyring):
            _keyring = None
            return None
        probe = "__nova_probe__"
        keyring.set_password(SERVICE, probe, "ok")
        if keyring.get_password(SERVICE, probe) != "ok":
            _keyring = None
            return None
        try:
            keyring.delete_password(SERVICE, probe)
        except Exception:
            pass
        _keyring = keyring
    except Exception:
        _keyring = None
    return _keyring


def backend() -> str:
    """'keyring:<backend name>' or 'file' — for honest reporting in the UI."""
    kr = _try_keyring()
    if kr is not None:
        try:
            return "keyring:" + type(kr.get_keyring()).__name__
        except Exception:
            return "keyring"
    return "file"


def is_hardware_backed() -> bool:
    return _try_keyring() is not None


# -- file fallback -----------------------------------------------------------

def _data_dir() -> Path:
    """Per-user application directory, on whichever platform this is."""
    if sys.platform == "win32":
        base = os.getenv("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        d = Path(base) / "NOVA"
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / "NOVA"
    else:
        base = os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        d = Path(base) / "nova"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fallback_path() -> Path:
    return _data_dir() / "secure_store.bin"


def _machine_key() -> bytes:
    """A key stored beside the data, readable only by this user.

    This protects against another *user* on the machine and against a file
    copied elsewhere. It cannot protect against code already running as this
    user -- nothing at this layer can, which is exactly why the OS keystore is
    preferred and this path is reported as weaker.
    """
    p = _data_dir() / "store.key"
    if p.exists():
        try:
            return base64.urlsafe_b64decode(p.read_bytes())
        except Exception:
            pass
    key = secrets.token_bytes(32)
    p.write_bytes(base64.urlsafe_b64encode(key))
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)     # 0600
    except Exception:
        pass
    return key


def _xor(data: bytes, key: bytes, nonce: bytes) -> bytes:
    out, counter = b"", 0
    while len(out) < len(data):
        out += hashlib.sha256(key + nonce + counter.to_bytes(4, "big")).digest()
        counter += 1
    return bytes(a ^ b for a, b in zip(data, out))


def _file_read() -> dict:
    p = _fallback_path()
    if not p.exists():
        return {}
    try:
        raw = base64.urlsafe_b64decode(p.read_bytes())
        nonce, mac, ct = raw[:16], raw[16:48], raw[48:]
        key = _machine_key()
        if not hmac.compare_digest(
                mac, hmac.new(key, nonce + ct, hashlib.sha256).digest()):
            return {}
        return json.loads(_xor(ct, key, nonce).decode("utf-8"))
    except Exception:
        return {}


def _file_write(data: dict) -> None:
    key = _machine_key()
    nonce = secrets.token_bytes(16)
    pt = json.dumps(data).encode("utf-8")
    ct = _xor(pt, key, nonce)
    mac = hmac.new(key, nonce + ct, hashlib.sha256).digest()
    p = _fallback_path()
    p.write_bytes(base64.urlsafe_b64encode(nonce + mac + ct))
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


# -- public API --------------------------------------------------------------

def set_secret(key: str, value: str) -> None:
    with _lock:
        kr = _try_keyring()
        if kr is not None:
            try:
                kr.set_password(SERVICE, key, value)
                return
            except Exception:
                pass                       # fall through to the file store
        data = _file_read()
        data[key] = value
        _file_write(data)


def get_secret(key: str) -> str | None:
    with _lock:
        kr = _try_keyring()
        if kr is not None:
            try:
                v = kr.get_password(SERVICE, key)
                if v is not None:
                    return v
            except Exception:
                pass
        return _file_read().get(key)


def delete_secret(key: str) -> None:
    with _lock:
        kr = _try_keyring()
        if kr is not None:
            try:
                kr.delete_password(SERVICE, key)
            except Exception:
                pass
        data = _file_read()
        if key in data:
            data.pop(key, None)
            _file_write(data)


def set_json(key: str, obj) -> None:
    set_secret(key, json.dumps(obj))


def get_json(key: str):
    raw = get_secret(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def app_dir() -> Path:
    """Exported so the rest of NOVA has one cross-platform data directory."""
    return _data_dir()


__all__ = ["set_secret", "get_secret", "delete_secret", "set_json", "get_json",
           "backend", "is_hardware_backed", "app_dir", "SERVICE"]
