"""nova_voice — the one voice behaviour model for NOVA.

There is one NOVA. The terminal (`python nova.py` -> live_extra.NOVALive) and
the desktop (desk.live_session.LiveManager) are two adapters around the same
conversational behaviour, and this module is that behaviour: everything about
*how NOVA listens and speaks* that must be identical on every surface.

It deliberately owns no transport. It knows nothing about Gemini, WebSockets,
Flask or pywebview — callers feed it mic frames and speaking state, and it
answers "what should I send" and "did the user just interrupt". That is what
lets one policy serve a blocking terminal loop and an event-driven desktop
server without either forking the rules.

The policy is taken verbatim from the terminal implementation, which is the
proven reference:

  * While NOVA speaks, the mic is muted — silence is sent instead of real
    audio, so NOVA never transcribes her own voice.
  * Silence is *sent*, not withheld: the Live socket drops with a 1011
    keepalive timeout if nothing arrives, which is exactly the reconnect loop
    the desktop was suffering.
  * A short cooldown after speech ends keeps the tail of NOVA's own audio out
    of the mic.
  * Barge-in: sustained speech over NOVA (RMS above threshold for N
    consecutive frames) interrupts her — playback queues are drained and real
    audio starts flowing again so the model also stops generating.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

import numpy as np

# ── Policy constants ─────────────────────────────────────────────────────────
# Taken from the terminal implementation. Deliberately not configurable yet:
# behavioural parity with the proven path comes first, tuning comes after real
# use. See live_extra.NOVALive._listen_audio.

BARGE_IN_RMS = 350.0        # int16 RMS above this counts as speech
BARGE_IN_CHUNKS = 2         # consecutive loud frames before interrupting
SPEAK_COOLDOWN_S = 0.25     # ignore mic this long after NOVA stops speaking

SEND_RATE = 16000           # mic -> model
RECEIVE_RATE = 24000        # model -> speaker
CHANNELS = 1


def frame_rms(frame: np.ndarray) -> float:
    """RMS amplitude of an int16 frame, as the terminal computes it."""
    if frame is None or len(frame) == 0:
        return 0.0
    return float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))


class VoiceGate:
    """Decides, per mic frame, what NOVA should send to the model.

    This is the whole listening policy in one place. Callers do:

        gate = VoiceGate(on_barge_in=stop_playback)
        gate.set_speaking(True)          # when NOVA starts talking
        payload = gate.process(frame)    # every mic callback
        gate.set_speaking(False)         # when NOVA stops

    ``process`` always returns bytes to transmit — real audio when NOVA is
    listening, silence when she is speaking. Never return None: withholding
    frames is what kills the Live socket.
    """

    def __init__(
        self,
        chunk_samples: int,
        on_barge_in: Optional[Callable[[], None]] = None,
        barge_in_rms: float = BARGE_IN_RMS,
        barge_in_chunks: int = BARGE_IN_CHUNKS,
        cooldown_s: float = SPEAK_COOLDOWN_S,
    ):
        self._silence = bytes(chunk_samples * 2)      # int16 = 2 bytes/sample
        self._on_barge_in = on_barge_in
        self._barge_in_rms = barge_in_rms
        self._barge_in_chunks = barge_in_chunks
        self._cooldown_s = cooldown_s

        self._lock = threading.Lock()
        self._speaking = False
        self._muted = False
        self._last_speak_end = 0.0
        self._speech_runs = 0

        # Counters, so a surface can report honestly instead of guessing.
        self.frames_sent = 0
        self.frames_muted = 0
        self.barge_ins = 0

    # ── state ────────────────────────────────────────────────────────────

    @property
    def speaking(self) -> bool:
        with self._lock:
            return self._speaking

    def set_speaking(self, value: bool) -> None:
        with self._lock:
            self._speaking = bool(value)
            if not value:
                self._last_speak_end = time.time()
                self._speech_runs = 0

    @property
    def muted(self) -> bool:
        """User-controlled mute. Distinct from the automatic speak-mute."""
        with self._lock:
            return self._muted

    def set_muted(self, value: bool) -> None:
        with self._lock:
            self._muted = bool(value)

    # ── the policy ───────────────────────────────────────────────────────

    def process(self, frame: np.ndarray) -> bytes:
        """Return the bytes to transmit for this mic frame."""
        with self._lock:
            if self._muted:
                # Explicit user mute: still send silence so the session stays
                # alive and NOVA can resume instantly when unmuted.
                self.frames_muted += 1
                return self._silence
            speaking = self._speaking
            too_soon = (time.time() - self._last_speak_end) < self._cooldown_s

        if not (speaking or too_soon):
            self.frames_sent += 1
            return frame.tobytes()

        # NOVA is talking (or just stopped). Mute by default, but listen for a
        # genuine interruption.
        if speaking and frame_rms(frame) > self._barge_in_rms:
            with self._lock:
                self._speech_runs += 1
                triggered = self._speech_runs >= self._barge_in_chunks
                if triggered:
                    self._speech_runs = 0
            if triggered:
                self.barge_ins += 1
                self.set_speaking(False)
                if self._on_barge_in is not None:
                    try:
                        self._on_barge_in()
                    except Exception:
                        pass
                self.frames_sent += 1
                return frame.tobytes()
        else:
            with self._lock:
                self._speech_runs = 0

        self.frames_muted += 1
        return self._silence

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "speaking": self._speaking,
                "muted": self._muted,
                "frames_sent": self.frames_sent,
                "frames_muted": self.frames_muted,
                "barge_ins": self.barge_ins,
            }


def drain(*queues) -> int:
    """Empty playback queues immediately. Used on barge-in.

    Accepts asyncio.Queue and queue.Queue alike; anything that raises is
    skipped, because a half-drained queue is still better than audio that
    keeps playing over the user.
    """
    dropped = 0
    for q in queues:
        if q is None:
            continue
        # queue.Queue — clear under its own mutex
        mutex = getattr(q, "mutex", None)
        inner = getattr(q, "queue", None)
        if mutex is not None and inner is not None:
            try:
                with mutex:
                    dropped += len(inner)
                    inner.clear()
                continue
            except Exception:
                pass
        # asyncio.Queue / anything with get_nowait
        getter = getattr(q, "get_nowait", None)
        if getter is None:
            continue
        while True:
            try:
                getter()
                dropped += 1
            except Exception:
                break
    return dropped


__all__ = [
    "VoiceGate", "drain", "frame_rms",
    "BARGE_IN_RMS", "BARGE_IN_CHUNKS", "SPEAK_COOLDOWN_S",
    "SEND_RATE", "RECEIVE_RATE", "CHANNELS",
]
