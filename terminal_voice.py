"""terminal_voice — NOVA's voice in a terminal, over the one voice session.

`python nova.py` used to run its own realtime implementation (``NOVALive`` in
live_extra.py), separate from the one the desktop app uses. They drifted, as
two implementations of the same thing do, and the terminal kept the older
behaviour: no batching of microphone frames, so fifteen WebSocket messages a
second and a send queue that could not keep up. Measured from a real session,
a quarter of everything the microphone heard was thrown away before it was
ever transmitted:

    [MIC] out_queue FULL — dropped chunk #1000 (consumer is falling behind)
    [MIC] 4000 callbacks total | sent=4000 dropped_queue_full=971

Audio that is never sent cannot be understood, which is why the terminal was
the worst place to talk to NOVA and why "she cannot hear me" was reported
there first.

So this is not a second implementation. It is a thin surface over
:class:`desk.live_session.LiveManager` — the same session, microphone,
speaker, tools and event stream the desktop window uses — rendered as text on
a console. Fixing the pipeline now fixes it everywhere, which is the whole
reason for the file.
"""
from __future__ import annotations

import asyncio
import queue
import sys
import threading
import time
from typing import Any

from desk import live_session as _live

#: How long to wait for the session to come up before saying it has not.
READY_TIMEOUT_S = 45.0

#: How long a network outage is tolerated before handing back to the caller so
#: NOVA can fall into offline mode. Long enough to ride out a lift or a train
#: tunnel; short enough that a genuinely dead connection is not a hang.
OFFLINE_GRACE_S = 60.0


class TerminalVoice:
    """A console view of the one voice session.

    Mirrors the small surface ``nova.py`` expects of the old implementation:
    ``speak(text)`` and ``await run()`` returning ``"exit"`` or ``"offline"``.
    """

    def __init__(self, meta: dict | None = None):
        self.meta = meta or {}
        self._mgr = _live.get_live_manager()
        self._events: queue.Queue | None = None
        self._stop = threading.Event()
        self._offline_since = 0.0
        self._said: list[str] = []
        self._heard: list[str] = []
        self._ready = threading.Event()
        #: Anything typed before the session was ready. Dropping it looks
        #: exactly like NOVA ignoring you, and the window in which it happens
        #: is the one where a user is most likely to be typing: the seconds
        #: after launch, while she is still connecting.
        self._pending: list[str] = []
        self._pending_lock = threading.Lock()

    # ── what nova.py calls ────────────────────────────────────────────────

    def speak(self, text: str) -> None:
        """Have NOVA say something unprompted (heartbeat, planner, notices)."""
        self._send(str(text or ""))

    def _send(self, text: str) -> None:
        """Send now, or hold it until the session is up."""
        if not text:
            return
        if not self._ready.is_set():
            with self._pending_lock:
                self._pending.append(text)
            return
        try:
            result = self._mgr.send_text(text)
            if not result.get("ok"):
                # Not connected after all -- hold it rather than lose it.
                with self._pending_lock:
                    self._pending.append(text)
        except Exception as e:
            print(f"[NOVA] could not send that: {e}")

    def _flush_pending(self) -> None:
        with self._pending_lock:
            waiting, self._pending = self._pending, []
        for text in waiting:
            try:
                self._mgr.send_text(text)
            except Exception:
                pass

    async def run(self) -> str:
        """Hold a conversation until the user stops it. Returns why it ended."""
        self._events = self._mgr.subscribe()
        result = self._mgr.start()
        if not result.get("ok"):
            print(f"\n[NOVA] ❌ Voice could not start: {result.get('reason')}")
            return "offline"

        print("\n[NOVA] 🔌 Connecting to Gemini Live…")
        typist = threading.Thread(target=self._read_typed, name="nova-stdin",
                                  daemon=True)
        typist.start()

        try:
            return await self._pump()
        except (KeyboardInterrupt, asyncio.CancelledError):
            return "exit"
        finally:
            self._stop.set()
            try:
                self._mgr.unsubscribe(self._events)
            except Exception:
                pass
            self._mgr.stop()

    # ── the console ───────────────────────────────────────────────────────

    def _read_typed(self) -> None:
        """Let the user type as well as talk.

        Reading stdin on its own thread rather than the event loop, because
        `input()` blocks until a newline and the audio must not wait for one.
        """
        while not self._stop.is_set():
            try:
                line = sys.stdin.readline()
            except Exception:
                return
            if not line:
                return                      # stdin closed
            text = line.strip()
            if not text:
                continue
            if text.lower() in ("quit", "exit", "goodbye nova"):
                self._stop.set()
                return
            self._send(text)

    async def _pump(self) -> str:
        """Render the session's events until it ends."""
        ready = False
        deadline = time.monotonic() + READY_TIMEOUT_S

        while not self._stop.is_set():
            # A voice session is mostly quiet, so this polls the subscriber
            # queue rather than blocking the loop on it.
            try:
                ev = self._events.get_nowait()
            except queue.Empty:
                if not ready and time.monotonic() > deadline:
                    print("[NOVA] ❌ Voice never became ready. "
                          "Run `python tools/voice_doctor.py` to find out why.")
                    return "offline"
                if (self._offline_since
                        and time.monotonic() - self._offline_since > OFFLINE_GRACE_S):
                    return "offline"
                await asyncio.sleep(0.05)
                continue

            kind = ev.type
            data = ev.data

            if kind == "state":
                state = data.get("state")
                if state == "ready":
                    ready = True
                    self._ready.set()
                    self._flush_pending()
                    self._offline_since = 0.0
                    print("[NOVA] ✅ Connected — speak, or type and press enter.")
                    print("[NOVA]    Ctrl-C to stop.\n")
                    if data.get("mic") is False:
                        print("[NOVA] ⚠️  No microphone — typing still works.")
                    if data.get("speaker") is False:
                        print("[NOVA] ⚠️  No speaker — you will not hear her.")
                elif state == "speaking":
                    self._offline_since = 0.0
                elif state == "offline":
                    if not self._offline_since:
                        self._offline_since = time.monotonic()
                        print("[NOVA] 📴 Network lost — waiting for it to come back…")
                elif state == "error":
                    msg = data.get("message") or data.get("error") or "unknown"
                    print(f"[NOVA] ❌ {msg}")
                    if data.get("fatal") or data.get("gave_up"):
                        return "offline"
                elif state == "closed":
                    return "exit"

            elif kind == "user_transcript":
                self._heard.append(data.get("text", ""))

            elif kind == "nova_transcript":
                self._said.append(data.get("text", ""))

            elif kind == "turn_complete":
                heard = " ".join(self._heard).strip()
                said = " ".join(self._said).strip()
                self._heard, self._said = [], []
                if heard:
                    print(f"🗣  You:  {heard}")
                if said:
                    print(f"🤖 NOVA: {said}\n")

            elif kind == "tool_call":
                tools = ", ".join(data.get("tools") or [])
                print(f"   🔧 {tools}")

            elif kind == "vision_capture":
                print(f"   👁  looking at your {data.get('source', 'screen')}")

            elif kind == "error":
                msg = data.get("message") or data.get("error")
                if msg:
                    print(f"[NOVA] ⚠️  {msg}")

        return "exit"


__all__ = ["TerminalVoice"]
