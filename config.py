"""NOVA configuration bridge.

Reads:
- nova_config.toml
- .env via dotenv

Exposes:
- get(dotpath, default)
- _find_config()
- _config / _config_path cache
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

try:
    import tomllib as _tomllib
except ImportError:
    _tomllib = None  # type: ignore[misc]

try:
    from dotenv import load_dotenv as _load_dotenv
except ImportError:
    _load_dotenv = None  # type: ignore[misc]

_loaded_env = False


def _ensure_env() -> None:
    global _loaded_env
    if not _loaded_env:
        if _load_dotenv is not None:
            _load_dotenv()
        _loaded_env = True


_config_path: Optional[Path] = None
_config: Optional[dict] = None


def _find_config() -> Path:
    return Path("nova_config.toml")


def _load_config() -> dict:
    path = _find_config()
    if path.exists():
        if _tomllib is not None:
            try:
                return _tomllib.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
    return {}


def get(dotpath: str, default: Any = None) -> Any:
    _ensure_env()
    global _config
    if _config is None:
        _config = _load_config()
    keys = dotpath.split(".")
    cur: Any = _config
    for key in keys:
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return default
    return cur if cur is not None else default
