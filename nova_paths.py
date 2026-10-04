"""One answer to "where does NOVA's runtime state live?".

The rule, which `nova.py` and `nova_safety.py` already each implement
separately:

1. `NOVA_DATA_DIR` if it is set — the test suite points this at a throwaway
   directory so a test run cannot write into the developer's own memory file.
2. Otherwise, once frozen, `%APPDATA%\\NOVA` — because the installer promises
   that user data never lives inside the install directory. Program Files is
   not writable by a standard user, and an uninstall must not take the
   person's memories with it.
3. Otherwise the working directory, which for a dev run is the repository.

Resolved on every call rather than at import, so a caller (or a test) can
change the environment and be obeyed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["data_dir", "data_file"]


def data_dir() -> Path:
    """The directory NOVA may write user state into."""
    override = os.getenv("NOVA_DATA_DIR", "").strip()
    if override:
        return _usable(Path(override))

    if getattr(sys, "frozen", False):
        appdata = os.getenv("APPDATA")
        if appdata:
            return _usable(Path(appdata) / "NOVA")
        return _usable(Path.home() / ".nova")

    return Path(".")


def data_file(name: str) -> Path:
    """Where a named state file belongs."""
    return data_dir() / name


def _usable(path: Path) -> Path:
    """Return `path`, creating it; fall back to the working directory.

    A path we cannot create is worse than useless: callers persist through it,
    and several of them swallow write errors, so an unwritable directory turns
    into state that silently never saves.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        return path
    except OSError:
        return Path(".")
