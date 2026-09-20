"""Asking permission where the person can actually answer.

From a real session: NOVA decided to shut the computer down, the gate asked
whether it should, and the question went to `input()` on a terminal nobody was
watching while the user was mid-sentence. NOVA froze for fifteen seconds
waiting to be typed at, took the silence as "no" -- correctly -- and said
nothing about any of it. From the user's side she simply stopped responding.

The refusal was right. The channel was wrong. A voice-first assistant asked a
question its user had no way to answer.

So when a live voice session exists the question is spoken into it, the answer
is the next thing the person says, and a timeout is announced rather than
swallowed. stdin remains the fallback for a terminal with no voice session,
which is the only place it was ever appropriate.

Silence still means no. That part was never in question: an unanswered request
to shut down a computer is a refusal.
"""
from __future__ import annotations

import logging
import queue
import time
from typing import Any, Callable, Optional

log = logging.getLogger("nova.confirm")

__all__ = ["VoiceConfirmer"]

#: Long enough to hear a question, think, and answer; short enough that NOVA
#: does not look frozen. The incident that prompted this had her silent for
#: fifteen seconds with no explanation.
DEFAULT_TIMEOUT_S = 20.0


def _default_live():
    try:
        from desk import live_session as _live
        return _live.get_live_manager()
    except Exception:
        return None


class VoiceConfirmer:
    """Speaks a confirmation question and listens for the reply."""

    def __init__(self, live_factory: Optional[Callable[[], Any]] = None,
                 timeout_seconds: float = DEFAULT_TIMEOUT_S) -> None:
        self._live_factory = live_factory or _default_live
        self.timeout_seconds = float(timeout_seconds)

    # ── plumbing ────────────────────────────────────────────────────────────
    def _session(self):
        """A live session that is actually able to carry a question."""
        try:
            manager = self._live_factory()
            if manager is None:
                return None
            state = (manager.status() or {}).get("state")
            if state not in ("connected", "streaming"):
                return None
            return manager
        except Exception:
            # Never let a broken session stop the gate from asking somehow.
            return None

    def speak(self, text: str) -> None:
        """Say the question. Used as safety_gate's speak_fn."""
        manager = self._session()
        if manager is None:
            print(f"\n[NOVA] {text}")
            return
        try:
            manager.send_text(text)
        except Exception:
            log.debug("[CONFIRM] could not speak the question", exc_info=True)
            print(f"\n[NOVA] {text}")

    # ── the answer ──────────────────────────────────────────────────────────
    def ask(self) -> str:
        """Wait for a reply. Empty means no, and says so."""
        manager = self._session()
        if manager is None:
            return self._ask_by_typing()

        events = None
        try:
            # Subscribe *after* deciding to ask, so an utterance from before
            # the question cannot be mistaken for the answer to it.
            events = manager.subscribe()
            deadline = time.time() + self.timeout_seconds
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                try:
                    event = events.get(timeout=min(0.25, remaining))
                except queue.Empty:
                    continue
                if getattr(event, "type", "") != "user_transcript":
                    continue
                text = (getattr(event, "data", {}) or {}).get("text", "")
                text = str(text or "").strip()
                if text:
                    return text
        except Exception:
            log.debug("[CONFIRM] listening failed", exc_info=True)
        finally:
            if events is not None:
                try:
                    manager.unsubscribe(events)
                except Exception:
                    pass

        # Say so. Going quiet is what made the original incident look like a
        # crash rather than a refusal.
        self.speak("I didn't hear an answer, so I've cancelled that.")
        return ""

    def _ask_by_typing(self) -> str:
        try:
            return input("NOVA awaiting your yes/no: ").strip()
        except (EOFError, KeyboardInterrupt):
            return ""
        except Exception:
            return ""
