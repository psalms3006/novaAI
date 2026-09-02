"""
Structured audit logger (NDJSON) with redaction.

Replaces the plain-text append of ``nova_safety._log`` for orchestrator
activity: every decision/execution path writes one structured JSON line with:
event, goal, tool, risk, decision, outcome, latency. Sensitive payloads are
redacted (secrets keys dropped, values truncated) — never logged in full.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("nova.orchestrator.audit")

_SECRET_KEY_PATTERN = re.compile(
    r"(key|token|secret|password|passwd|credentials?|authorization|auth|bearer|api[_-]?key)",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERN = re.compile(
    r"(sk-|AIza|AKIA|ghp_|xox[baprs]-|-----BEGIN)", re.IGNORECASE
)
_IMPLICIT_PRIVATE_KEYS = {"content", "message_text", "message", "body", "code", "old_code", "new_code"}

_MAX_VALUE_CHARS = 200


def redact_value(value: Any) -> Any:
    """Redact a single value: secrets → '<redacted>', long strings → truncated."""
    if isinstance(value, str):
        if _SECRET_VALUE_PATTERN.search(value):
            return "<redacted>"
        return value if len(value) <= _MAX_VALUE_CHARS else value[:_MAX_VALUE_CHARS] + "..."
    if isinstance(value, dict):
        return redact_payload(value)
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    return value


def redact_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    """Redact sensitive keys recursively; drop private message contents."""
    out: Dict[str, Any] = {}
    for key, value in data.items():
        lower = key.lower()
        if _SECRET_KEY_PATTERN.search(lower):
            continue
        if lower in _IMPLICIT_PRIVATE_KEYS and isinstance(value, str):
            out[key] = f"<private:{len(value)} chars>"
            continue
        out[key] = redact_value(value)
    return out


class AuditLogger:
    """Append-only structured audit store."""

    def __init__(self, path: Optional[str] = None, max_lines: int = 2000) -> None:
        self.path = Path(path) if path else Path("data") / "nova_audit.ndjson"
        self.max_lines = max_lines
        self._lock = threading.Lock()
        if not self.path.exists():
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text("", encoding="utf-8")
            except OSError as exc:  # data dir may be read-only
                log.warning("Audit file unavailable (%s); audit disabled.", exc)

    def record(self, event: str, **data: Any) -> Dict[str, Any]:
        """Write one structured audit record; returns the redacted record."""
        entry = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "ts_unix": time.time(),
            "event": event,
            **redact_payload(data),
        }
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError as exc:
                log.debug("Audit write failed: %s", exc)
            if self.path.exists():  # flush/close happened above; trim deterministically
                self._trim()
        return entry

    def _trim(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            if len(lines) > self.max_lines:
                self.path.write_text("\n".join(lines[-self.max_lines:]) + "\n", encoding="utf-8")
        except OSError:
            pass

    def count(self) -> int:
        """Best-effort total row count of the audit file."""
        if not self.path.exists():
            return 0
        try:
            return len(self.path.read_text(encoding="utf-8", errors="ignore").splitlines())
        except OSError:
            return 0

    def recent(self, limit: int = 20, event: Optional[str] = None) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out: List[Dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event and record.get("event") != event:
                continue
            out.append(record)
        return out