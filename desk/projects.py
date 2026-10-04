"""desk.projects — persistent NOVA workspace projects.

A project is a persistent workspace around a subject/task: it carries a name,
description, free-form instructions that are injected into NOVA's system prompt
while that project is active, and links to its conversations. Persisted as JSON
in the app data directory (source of truth is the backend, not the browser).
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from .settings import app_data_dir

_LOCK = threading.Lock()
def _path() -> Path:
    # Per call: the folder is the signed-in account's (see desk.settings).
    d = app_data_dir() / "projects"
    d.mkdir(parents=True, exist_ok=True)
    return d / "projects.json"


def _load() -> list:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def _save(projects: list) -> None:
    try:
        _path().write_text(json.dumps(projects, indent=2, ensure_ascii=False),
                           encoding="utf-8")
    except Exception:
        pass


def list_projects() -> list:
    with _LOCK:
        return list(_load())


def get_project(pid: str) -> dict | None:
    with _LOCK:
        for p in _load():
            if p.get("id") == pid:
                return p
    return None


def create_project(name: str, description: str = "", instructions: str = "",
                   color: str = "violet") -> dict:
    name = (name or "").strip() or "Untitled project"
    now = time.time()
    proj = {
        "id": uuid.uuid4().hex,
        "name": name,
        "description": (description or "").strip(),
        "instructions": (instructions or "").strip(),
        "color": color or "violet",
        "created": now,
        "updated": now,
    }
    with _LOCK:
        projects = _load()
        projects.append(proj)
        _save(projects)
    return proj


def update_project(pid: str, **kw) -> dict | None:
    with _LOCK:
        projects = _load()
        for p in projects:
            if p.get("id") == pid:
                if "name" in kw:
                    p["name"] = (kw["name"] or "").strip() or p["name"]
                if "description" in kw:
                    p["description"] = (kw["description"] or "").strip()
                if "instructions" in kw:
                    p["instructions"] = (kw["instructions"] or "").strip()
                if "color" in kw:
                    p["color"] = kw["color"] or p["color"]
                p["updated"] = time.time()
                _save(projects)
                return p
    return None


def delete_project(pid: str) -> bool:
    with _LOCK:
        projects = _load()
        kept = [p for p in projects if p.get("id") != pid]
        if len(kept) == len(projects):
            return False
        _save(kept)
    return True