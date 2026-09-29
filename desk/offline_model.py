"""desk.offline_model — prepare NOVA's offline model, with real progress.

The model runs on Ollama. Which model to recommend comes from the NOVA
backend's catalog (/v1/models/offline), so it can change without a release;
the built-in copy below is used when the backend cannot be reached.

Progress is Ollama's own byte counts from /api/pull, summed over layers --
never a timer. Ollama checks each downloaded layer against its sha256 digest
and refuses a mismatch; after a pull, the model must also appear in /api/tags
before NOVA calls it ready. A failure never blocks onboarding: the state says
what happened and the person can retry from Settings.

    GET  /api/offline-model            catalog, hardware fit, current state
    POST /api/offline-model/download   {"model": id}   start (or resume)
    POST /api/offline-model/cancel
"""
from __future__ import annotations

import json
import os
import threading
import time

from flask import jsonify, request

OLLAMA = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")

BUILTIN_CATALOG = {
    "runtime": {"name": "ollama", "installer_url": "https://ollama.com/download/OllamaSetup.exe",
                "installer_sha256": "", "installer_size_bytes": 0},
    "recommended": "llama3.2:1b",
    "models": [
        {"id": "llama3.2:1b", "name": "Llama 3.2 1B", "size_bytes": 1_300_000_000,
         "min_ram_gb": 4, "note": "Fast, basic offline help."},
        {"id": "qwen2.5:3b", "name": "Qwen 2.5 3B", "size_bytes": 1_900_000_000,
         "min_ram_gb": 8, "note": "Better answers, needs more memory."},
    ],
}

_lock = threading.Lock()
_state: dict = {"state": "idle", "model": "", "completed_bytes": 0, "total_bytes": 0,
                "status_text": "", "error": "", "started_at": 0.0, "finished_at": 0.0}
_cancel = threading.Event()


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def state() -> dict:
    with _lock:
        s = dict(_state)
    s["percent"] = (round(100 * s["completed_bytes"] / s["total_bytes"], 1)
                    if s["total_bytes"] else None)
    return s


def catalog() -> dict:
    try:
        import nova_account
        acct = nova_account.account()
        if acct.configured:
            j = acct._request("GET", "/v1/models/offline", timeout=5)
            if isinstance(j.get("catalog"), dict) and j["catalog"].get("models"):
                return j["catalog"]
    except Exception:
        pass
    return BUILTIN_CATALOG


def total_ram_gb() -> float | None:
    try:
        import psutil
        return round(psutil.virtual_memory().total / 1024 ** 3, 1)
    except Exception:
        return None


def runtime_available() -> bool:
    import requests
    try:
        return requests.get(OLLAMA + "/api/tags", timeout=2).status_code == 200
    except Exception:
        return False


def installed_models() -> list[str]:
    import requests
    try:
        return [m.get("name", "") for m in
                requests.get(OLLAMA + "/api/tags", timeout=3).json().get("models", [])]
    except Exception:
        return []


def _is_installed(model: str) -> bool:
    names = installed_models()
    base = model if ":" in model else model + ":latest"
    return model in names or base in names


def _pull(model: str) -> None:
    import requests
    layers: dict[str, tuple[int, int]] = {}
    try:
        with requests.post(OLLAMA + "/api/pull", json={"model": model, "stream": True},
                           stream=True, timeout=(10, 600)) as r:
            if r.status_code != 200:
                raise RuntimeError(f"Ollama refused the download ({r.status_code})")
            for line in r.iter_lines():
                if _cancel.is_set():
                    raise RuntimeError("cancelled")
                if not line:
                    continue
                ev = json.loads(line)
                if ev.get("error"):
                    # Includes digest mismatches: Ollama verifies every layer.
                    raise RuntimeError(ev["error"])
                if ev.get("digest") and ev.get("total"):
                    layers[ev["digest"]] = (int(ev.get("completed") or 0), int(ev["total"]))
                done = sum(c for c, _ in layers.values())
                total = sum(t for _, t in layers.values())
                _set(completed_bytes=done, total_bytes=total,
                     status_text=str(ev.get("status") or ""))
                if ev.get("status") == "success":
                    break
        _set(state="verifying", status_text="Checking the model is installed")
        if not _is_installed(model):
            raise RuntimeError("the download finished but Ollama does not list the model")
        _set(state="ready", finished_at=time.time(), status_text="Ready")
    except Exception as e:
        msg = str(e)
        _set(state="cancelled" if msg == "cancelled" else "failed",
             error=msg, finished_at=time.time())


def start(model: str) -> dict:
    with _lock:
        if _state["state"] in ("downloading", "verifying"):
            return dict(_state)
    known = {m["id"] for m in catalog().get("models", [])}
    if model not in known:
        raise ValueError(f"{model} is not in NOVA's offline model catalog")
    if not runtime_available():
        _set(state="runtime_missing", model=model, error=(
            "NOVA's offline engine (Ollama) is not installed or not running. Install it "
            "from ollama.com, then try again from Settings."))
        return state()
    if _is_installed(model):
        _set(state="ready", model=model, error="", status_text="Already installed",
             finished_at=time.time())
        return state()
    _cancel.clear()
    _set(state="downloading", model=model, completed_bytes=0, total_bytes=0, error="",
         status_text="Starting", started_at=time.time(), finished_at=0.0)
    threading.Thread(target=_pull, args=(model,), name="nova-offline-pull", daemon=True).start()
    return state()


def register(app, require_token) -> None:
    @app.get("/api/offline-model")
    @require_token
    def api_offline_model():
        cat = catalog()
        ram = total_ram_gb()
        models = [{**m, "fits": (ram is None or ram >= float(m.get("min_ram_gb") or 0)),
                   "installed": _is_installed(m["id"])} for m in cat.get("models", [])]
        return jsonify({"ok": True, "recommended": cat.get("recommended"), "models": models,
                        "ram_gb": ram, "runtime_available": runtime_available(),
                        "runtime": cat.get("runtime", {}), "state": state()})

    @app.post("/api/offline-model/download")
    @require_token
    def api_offline_model_download():
        model = str((request.get_json(silent=True) or {}).get("model") or "").strip()
        try:
            return jsonify({"ok": True, "state": start(model)})
        except ValueError as e:
            return jsonify({"ok": False, "error": "unknown_model", "message": str(e)}), 400

    @app.post("/api/offline-model/cancel")
    @require_token
    def api_offline_model_cancel():
        _cancel.set()
        return jsonify({"ok": True, "state": state()})


__all__ = ["register", "state", "start", "catalog", "BUILTIN_CATALOG"]
