"""A written record of every time NOVA changed herself, or tried to.

Kept separately from the code it describes, and append-only, because the
question it answers is asked after something has gone wrong: what changed,
when, on whose say-so, what was run against it, and whether the undo worked.
An audit trail that lives inside the thing being audited answers that
question only while nothing is seriously broken.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


def journal_dir() -> Path:
    env = os.getenv("NOVA_DATA_DIR", "").strip()
    if env:
        return Path(env) / "self"
    appdata = os.getenv("APPDATA")
    if appdata:
        return Path(appdata) / "NOVA" / "self"
    return Path("./nova_self_journal")


@dataclass
class Attempt:
    """One self-improvement, from the problem to whatever became of it."""

    attempt_id: str
    problem: str
    rationale: str = ""
    risk: str = "medium"                 # low | medium | high
    files: list[str] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)
    #: proposed | tested | awaiting_approval | applied | rejected |
    #: rolled_back | failed
    status: str = "proposed"
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)

    tests_command: str = ""
    tests_passed: Optional[int] = None
    tests_failed: Optional[int] = None
    tests_ok: Optional[bool] = None
    tests_output: str = ""

    authorised_by: str = ""
    baseline_commit: str = ""
    snapshot: str = ""
    rollback_status: str = ""
    notes: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class Journal:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else journal_dir() / "attempts.jsonl"
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, attempt: Attempt) -> None:
        """Append the current state of an attempt.

        Append rather than update: the history of an attempt is part of the
        record. "Tested, then applied, then rolled back forty seconds later"
        is the interesting shape, and rewriting a single row erases it.
        """
        attempt.updated = time.time()
        line = json.dumps(attempt.to_json(), ensure_ascii=False)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def entries(self, limit: int = 100) -> list[dict]:
        if not self.path.is_file():
            return []
        out: list[dict] = []
        with self._lock:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
        return out[-limit:]

    def latest(self, attempt_id: str) -> Optional[dict]:
        """The most recent state recorded for one attempt."""
        found = None
        for row in self.entries(limit=10_000):
            if row.get("attempt_id") == attempt_id:
                found = row
        return found

    def history(self, attempt_id: str) -> list[dict]:
        return [r for r in self.entries(limit=10_000)
                if r.get("attempt_id") == attempt_id]

    def summary(self, limit: int = 20) -> list[dict]:
        """One row per attempt, newest first — for a report or a screen."""
        latest: dict[str, dict] = {}
        for row in self.entries(limit=10_000):
            latest[row.get("attempt_id", "")] = row
        rows = sorted(latest.values(), key=lambda r: r.get("updated", 0),
                      reverse=True)
        return [{
            "attempt_id": r.get("attempt_id"),
            "problem": r.get("problem", "")[:120],
            "status": r.get("status"),
            "risk": r.get("risk"),
            "files": r.get("files", []),
            "protected": r.get("protected", []),
            "tests_ok": r.get("tests_ok"),
            "authorised_by": r.get("authorised_by", ""),
            "rollback_status": r.get("rollback_status", ""),
            "updated": r.get("updated"),
        } for r in rows[:limit]]
