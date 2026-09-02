import json
import os
import signal
import sys
import time
import threading
from datetime import datetime
from pathlib import Path


def _default_memory_dir() -> Path:
    """Session files belong with the user's data, not the install directory.

    Frozen (installed) builds write to %APPDATA%\\NOVA\\nova_memories so that
    uninstalling/upgrade-replacing the install folder never touches session
    history. Development runs keep the historical ./nova_memories folder.
    """
    if getattr(sys, "frozen", False):
        base = os.getenv("APPDATA")
        if base:
            return Path(base) / "NOVA" / "nova_memories"
    return Path("nova_memories")


class NovaMemory:
    def __init__(self, memory_dir=None, checkpoint_every_n_turns=10,
                 autosave_seconds=120, summarizer_fn=None):
        """
        checkpoint_every_n_turns: write a snapshot to the archive after this many
            turns, even if the session never closes cleanly. Set 0 to disable.
        summarizer_fn: optional callable(turns: list[dict]) -> str. If provided,
            used instead of the crude truncation summary (e.g. wire in your
            Gemini/Mistral call here for real summaries).
        """
        self.memory_dir = Path(memory_dir) if memory_dir else _default_memory_dir()
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        self.current_session = {
            "started": datetime.now().isoformat(),
            "turns": [],
            "topics": set()
        }
        self.session_file = self.memory_dir / "current_session.json"
        self.archive_file = self.memory_dir / "session_archive.jsonl"
        self.latest_summary_file = self.memory_dir / "latest_closed_summary.json"
        self.checkpoint_every_n_turns = checkpoint_every_n_turns
        self.summarizer_fn = summarizer_fn
        self._turns_since_checkpoint = 0
        self._closed = False

        # Recover any previous crashed session before starting the new one
        self._recover_crash_session()

        self._autosave_seconds = autosave_seconds
        self._stop_event = threading.Event()
        self._saver = threading.Thread(target=self._autosave_loop, daemon=True)
        self._saver.start()

        try:
            signal.signal(signal.SIGINT, self._signal_handler)
            signal.signal(signal.SIGTERM, self._signal_handler)
        except ValueError:
            # Not in main thread — caller must handle shutdown manually.
            pass

    def _signal_handler(self, signum, frame):
        self.close_session()
        sys.exit(0)

    def log_turn(self, role: str, content: str):
        """Call this after EVERY exchange."""
        turn = {
            "timestamp": time.time(),
            "role": role,
            "content": content[:500]
        }
        self.current_session["turns"].append(turn)

        if any(k in content.lower() for k in ["fix", "bug", "error", "patch", "tool"]):
            self.current_session["topics"].add("development")
        if any(k in content.lower() for k in ["search", "web", "google", "api"]):
            self.current_session["topics"].add("websearch")

        self._turns_since_checkpoint += 1
        if self.checkpoint_every_n_turns and self._turns_since_checkpoint >= self.checkpoint_every_n_turns:
            self._checkpoint_to_archive()
            self._turns_since_checkpoint = 0

    def _checkpoint_to_archive(self):
        """Append an in-progress snapshot. Does NOT touch latest_summary_file —
        checkpoints are not 'closed' sessions and must never be returned by
        get_last_session_summary()."""
        snapshot = {
            **self.current_session,
            "topics": list(self.current_session["topics"]),
            "status": "checkpoint",
            "checkpoint_at": datetime.now().isoformat(),
            "summary": self._summarize(self.current_session["turns"])
        }
        with open(self.archive_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(snapshot) + "\n")
        self._write_session_file()

    def _write_latest_summary_cache(self, closed_session: dict):
        """O(1) write. This is what get_last_session_summary() reads — it never
        touches the full archive on the happy path."""
        temp = self.latest_summary_file.with_suffix(".tmp")
        temp.write_text(json.dumps(closed_session), encoding="utf-8")
        temp.replace(self.latest_summary_file)

    def _autosave_loop(self):
        while not self._stop_event.is_set():
            self._stop_event.wait(self._autosave_seconds)
            if not self._stop_event.is_set():
                self._write_session_file()

    def _write_session_file(self):
        temp = self.session_file.with_suffix(".tmp")
        data = {
            **self.current_session,
            "topics": list(self.current_session["topics"]),
            "last_autosave": datetime.now().isoformat()
        }
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temp.replace(self.session_file)

    def _recover_crash_session(self):
        if not self.session_file.exists():
            return
        try:
            old = json.loads(self.session_file.read_text())
            old["ended"] = "CRASHED_OR_ABRUPT"
            old["status"] = "closed"
            old["summary"] = self._summarize(old.get("turns", []))

            with open(self.archive_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(old) + "\n")
            self._write_latest_summary_cache(old)

            backup = self.memory_dir / f"crashed_session_{int(time.time())}.json"
            self.session_file.replace(backup)
            print(f"[MEMORY] Recovered crashed session from {old['started']}")
        except Exception as e:
            print(f"[MEMORY] Recovery failed: {e}")

    def _summarize(self, turns):
        if self.summarizer_fn:
            try:
                return self.summarizer_fn(turns)
            except Exception as e:
                print(f"[MEMORY] summarizer_fn failed, falling back: {e}")
        return self._quick_summarize(turns)

    def _quick_summarize(self, turns):
        if not turns:
            return "Empty session"
        first = next((t for t in turns if t["role"] == "user"), None)
        recent = turns[-6:]
        parts = []
        if first:
            parts.append(f"Started with: {first['content'][:100]}")
        if recent:
            parts.append(f"Last discussed: {recent[-1]['content'][:100]}")
        return " | ".join(parts)

    def close_session(self):
        if self._closed:
            return
        self._closed = True
        self._stop_event.set()
        self._saver.join(timeout=1)

        self.current_session["ended"] = datetime.now().isoformat()
        self.current_session["status"] = "closed"
        self.current_session["summary"] = self._summarize(self.current_session["turns"])

        closed_record = {
            **self.current_session,
            "topics": list(self.current_session["topics"])
        }
        with open(self.archive_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(closed_record) + "\n")
        self._write_latest_summary_cache(closed_record)

        if self.session_file.exists():
            self.session_file.unlink()
        print("[MEMORY] Session archived.")

    def get_last_session_summary(self):
        """O(1): reads the small cache file only. Falls back to a full archive
        scan ONLY if the cache is missing or corrupted (e.g. manual file edits,
        first run on an old archive predating this version)."""
        if self.latest_summary_file.exists():
            try:
                sess = json.loads(self.latest_summary_file.read_text())
                return (f"Last session ({sess['started'][:10]}): {sess['summary']}. "
                        f"Topics: {', '.join(sess.get('topics', []))}. "
                        f"Status: {sess.get('ended', 'unknown')}")
            except Exception as e:
                print(f"[MEMORY] Cache read failed, falling back to archive scan: {e}")

        return self._fallback_scan_archive()

    def _fallback_scan_archive(self):
        """O(archive size). Only runs if the cache is unavailable. Also rebuilds
        the cache so the next call is O(1) again."""
        if not self.archive_file.exists():
            return "No previous sessions found."
        lines = self.archive_file.read_text().strip().split("\n")
        for line in reversed(lines):
            sess = json.loads(line)
            if sess.get("status") == "closed" or "ended" in sess:
                self._write_latest_summary_cache(sess)
                return (f"Last session ({sess['started'][:10]}): {sess['summary']}. "
                        f"Topics: {', '.join(sess.get('topics', []))}. "
                        f"Status: {sess.get('ended', 'unknown')}")
        return "No closed previous sessions found."

    def get_current_session_summary(self):
        """Already O(1) — in-memory only, no file access."""
        return self._summarize(self.current_session["turns"])

    def get_all_sessions_on_topic(self, topic):
        """Still O(archive size) — there's no cheaper way to search by topic
        across history without a real index (e.g. SQLite). Flagging this as
        the next thing to fix if topic search becomes a real-time call path."""
        if not self.archive_file.exists():
            return []
        matches = []
        for line in self.archive_file.read_text().strip().split("\n"):
            sess = json.loads(line)
            if topic in sess.get("topics", []):
                matches.append(sess["summary"])
        return matches