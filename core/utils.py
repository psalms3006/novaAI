"""
NOVA Core Utilities — Atomic file operations, safe helpers.

Inspired by Hermes' utils.py pattern for atomic file writes with fsync.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("nova.utils")


def atomic_json_write(path: Path | str, data: Any, indent: int = 2) -> None:
    """
    Write JSON data atomically using temp file + os.replace.
    Prevents corruption from partial writes or crashes.
    Preserves symlinks in the parent directory.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        suffix=".tmp",
        prefix=path.stem + ".",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        # os.replace is atomic on POSIX, near-atomic on Windows
        os.replace(tmp_path, str(path))
    except BaseException:
        # Clean up temp file on ANY exception (including KeyboardInterrupt)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_json_read(path: Path | str, default: Any = None) -> Any:
    """
    Read JSON data with corruption recovery.
    If the file is corrupted, moves it aside and returns default.
    """
    path = Path(path)
    if not path.exists():
        return default
    try:
        raw = path.read_text(encoding="utf-8")
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        corrupted = path.with_suffix(path.suffix + f".corrupted.{int(os.getpid())}")
        log.error(f"Corrupted JSON file: {path} — {e}. Moved to {corrupted}")
        try:
            shutil.move(str(path), str(corrupted))
        except OSError:
            pass
        return default


def safe_file_write(path: Path | str, content: str, encoding: str = "utf-8") -> None:
    """Write text content atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        suffix=".tmp",
        prefix=path.stem + ".",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def resolve_path(shortcut: str) -> Path:
    """Resolve common path shortcuts to actual paths."""
    home = Path.home()
    shortcuts = {
        "desktop": home / "Desktop",
        "downloads": home / "Downloads",
        "documents": home / "Documents",
        "home": home,
        "~": home,
        "nova": Path(__file__).parent.parent,
    }
    return shortcuts.get(shortcut.lower(), Path(shortcut))


def sanitize_shell_arg(arg: str) -> str:
    """
    Sanitize a string for safe use in shell commands.
    Returns the original string if it looks safe, raises ValueError otherwise.
    """
    # Block common shell metacharacters
    dangerous = set(';|&$`\\(){}[]<>!#~\'"')
    if any(c in dangerous for c in arg):
        raise ValueError(f"Potentially dangerous shell argument: {arg!r}")
    return arg


def truncate_text(text: str, max_length: int = 200, suffix: str = "...") -> str:
    """Truncate text to max_length, adding suffix if truncated."""
    if len(text) <= max_length:
        return text
    return text[: max_length - len(suffix)] + suffix


def chunk_list(lst: list, chunk_size: int) -> List[list]:
    """Split a list into chunks of specified size."""
    return [lst[i: i + chunk_size] for i in range(0, len(lst), chunk_size)]