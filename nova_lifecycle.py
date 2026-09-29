"""nova_lifecycle — installation, account folders and first-run state.

Separate facts, never one boolean (docs/NOVA_PRODUCTION_ARCHITECTURE.md §3):

    installation initialized   this PC has run NOVA before      lifecycle.json
    device setup completed     permissions/startup/offline      accounts/<id>/local_state.json
                               chosen by this account on this PC
    instance onboarding done   profile step, any device          server (/v1/instance)
    authenticated              a refreshable session exists      secure store (nova_account)

None of it depends on the app version, so installing an update cannot put
anyone back through setup.

Where data lives:

    %APPDATA%\\NOVA\\                machine level: lifecycle.json, device
                                    identity, secure store, models\\, data\\
                                    (knowledge), logs
    %APPDATA%\\NOVA\\accounts\\<id>\\  account level: settings, permissions,
                                    memory, conversations, tasks, documents

`activate_account()` must run before the brain (nova.py) is imported: that
module fixes its data paths from NOVA_DATA_DIR at import time. The desktop
therefore shows sign-in first and starts the brain afterwards, and a sign-out
restarts the process rather than trying to unload one person's state.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

SCHEMA = 1
_LOCK = threading.RLock()
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# What belonged to "the user" before there were accounts, and so moves into the
# first account's folder. An explicit list: anything unknown stays where it is
# rather than being guessed into someone's private space.
ADOPTABLE = (
    "settings.json", "nova_desktop.db", "memory_texts.json", "memory_meta.json",
    "memory.index", "living_memory.json", "nova_memories", "nova_tasks.json",
    "nova_tasks_aios.json", "workflows.json", "goals.json", "connected_accounts.json",
    "rag", "identity", "self", "projects", "workspace", "extensions",
    "byok.bin", "api_keys.json", "nova_audit.ndjson", "nova_reference_face.jpg",
    "heartbeat_inbox.json", "heartbeat_state.json",
)

_original_data_dir: str | None = None


def machine_dir() -> Path:
    override = os.getenv("NOVA_MACHINE_DIR", "").strip()
    if override:
        d = Path(override)
    elif os.getenv("APPDATA"):
        d = Path(os.environ["APPDATA"]) / "NOVA"
    else:
        d = Path.home() / ".nova"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path() -> Path:
    return machine_dir() / "lifecycle.json"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _blank() -> dict:
    return {"schema": SCHEMA, "installation_id": None, "installation_initialized_at": None,
            "last_account_id": None, "adopted_legacy_data_by": None,
            "last_run_version": None}


def load() -> dict:
    with _LOCK:
        try:
            data = json.loads(_path().read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {**_blank(), **data}
        except Exception:
            pass
        return _blank()


def _save(data: dict) -> None:
    with _LOCK:
        tmp = _path().with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, _path())


def _legacy_items() -> list[str]:
    m = machine_dir()
    return [name for name in ADOPTABLE if (m / name).exists()]


def is_first_run() -> bool:
    """True only on a machine that has never run NOVA.

    A missing or unreadable lifecycle.json is not proof of that: an install
    upgraded from a build without accounts has data but no lifecycle file, and
    that person must not be greeted as someone new.
    """
    if load().get("installation_initialized_at"):
        return False
    m = machine_dir()
    if (m / "accounts").is_dir() and any((m / "accounts").iterdir()):
        return False
    return not _legacy_items()


def ensure_installation(app_version: str) -> dict:
    with _LOCK:
        data = load()
        if not data.get("installation_id"):
            data["installation_id"] = str(uuid.uuid4())
        if not data.get("installation_initialized_at"):
            data["installation_initialized_at"] = _now_iso()
        data["last_run_version"] = app_version
        data["schema"] = SCHEMA
        _save(data)
        return data


def account_dir(account_id: str) -> Path:
    account_id = (account_id or "").strip()
    if not _ID.match(account_id):
        raise ValueError(f"not a usable account id: {account_id!r}")
    return machine_dir() / "accounts" / account_id


def _adopt_legacy(target: Path) -> list[str]:
    moved = []
    m = machine_dir()
    for name in _legacy_items():
        src, dst = m / name, target / name
        if dst.exists():
            continue                      # never overwrite the account's own copy
        try:
            os.replace(src, dst)          # same volume: atomic rename
        except OSError:
            if src.is_dir():
                shutil.copytree(src, dst)
                shutil.rmtree(src, ignore_errors=True)
            else:
                shutil.copy2(src, dst)
                src.unlink(missing_ok=True)
        moved.append(name)
    return moved


def activate_account(account_id: str) -> dict:
    """Bind this process to one account's data. Call before the brain starts."""
    global _original_data_dir
    target = account_dir(account_id)
    with _LOCK:
        target.mkdir(parents=True, exist_ok=True)
        data = load()
        adopted: list[str] = []
        if not data.get("adopted_legacy_data_by"):
            adopted = _adopt_legacy(target)
            # Recorded even when nothing was there, so files an old build
            # writes later are never pulled into whoever signs in next.
            data["adopted_legacy_data_by"] = account_id
        data["last_account_id"] = account_id
        _save(data)
    if _original_data_dir is None:
        _original_data_dir = os.environ.get("NOVA_DATA_DIR", "")
    os.environ["NOVA_DATA_DIR"] = str(target)
    os.environ["NOVA_ACCOUNT_ID"] = account_id
    return {"account_id": account_id, "dir": str(target), "adopted": adopted}


def deactivate() -> None:
    """Forget the active account (sign-out). The installation stays set up."""
    global _original_data_dir
    with _LOCK:
        data = load()
        data["last_account_id"] = None
        _save(data)
    os.environ.pop("NOVA_ACCOUNT_ID", None)
    if _original_data_dir is not None:
        if _original_data_dir:
            os.environ["NOVA_DATA_DIR"] = _original_data_dir
        else:
            os.environ.pop("NOVA_DATA_DIR", None)
        _original_data_dir = None


def active_account_id() -> str:
    return os.environ.get("NOVA_ACCOUNT_ID", "")


def active_account_dir() -> Path | None:
    aid = active_account_id()
    return account_dir(aid) if aid else None


def _local_state_path() -> Path | None:
    d = active_account_dir()
    return d / "local_state.json" if d else None


def _local_state() -> dict:
    p = _local_state_path()
    if p is None:
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def device_setup_completed() -> bool:
    return bool(_local_state().get("device_setup_completed_at"))


def mark_device_setup_complete() -> None:
    p = _local_state_path()
    if p is None:
        raise RuntimeError("no account is active")
    state = _local_state()
    state.setdefault("device_setup_completed_at", _now_iso())
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


__all__ = ["machine_dir", "load", "is_first_run", "ensure_installation", "account_dir",
           "activate_account", "deactivate", "active_account_id", "active_account_dir",
           "device_setup_completed", "mark_device_setup_complete", "ADOPTABLE"]
