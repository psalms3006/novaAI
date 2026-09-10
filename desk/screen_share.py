"""desk.screen_share — what NOVA can see, while she is listening.

Ambient mode is meant to answer "what is this?" about whatever is on screen,
conversationally, without the user stopping to take a screenshot. That means
the screen has to be an input to the *same* Gemini Live session the
conversation is already happening in — not a separate vision call that would
lose the thread and add a round trip.

The whole design problem is cost. A live session's uplink is already the
tightest resource in this pipeline (see MicBatcher in live_session), and a
full-resolution screenshot is two orders of magnitude larger than an audio
frame. Sending video at video rates would starve the audio and make NOVA
stutter — the opposite of the point.

So this samples rather than streams:

  * at most one frame every :data:`MIN_INTERVAL_S`, and
  * only when the screen has actually changed, measured on a tiny greyscale
    thumbnail that costs almost nothing, and
  * downscaled and JPEG-compressed to roughly the size of a second of audio.

An unchanged screen therefore costs nothing at all, and a busy one costs a
predictable trickle. The model keeps the most recent frames in its context,
so "what is this?" is answered from what the user is looking at now.

Privacy is a first-class constraint here, not a footnote. Capture only runs
while it has been explicitly switched on, frames are held in memory and never
written to disk, and the manager publishes its state so every surface can show
plainly that NOVA is watching.
"""
from __future__ import annotations

import io
import threading
import time
from typing import Any, Callable

import numpy as np

try:
    import mss
    HAS_MSS = True
except Exception:
    mss = None
    HAS_MSS = False

try:
    from PIL import Image
    HAS_PIL = True
except Exception:
    Image = None
    HAS_PIL = False


#: Never send frames faster than this. Two seconds is well inside
#: conversational reaction time — by the time someone has asked "what is
#: this?", the frame they are asking about has already been sent — and it caps
#: the bandwidth cost at something the audio stream can live alongside.
MIN_INTERVAL_S = 2.0

#: Send an unchanged screen at least this often anyway, so the model's view
#: cannot quietly go stale while someone reads a long page.
MAX_INTERVAL_S = 30.0

#: Longest edge of a transmitted frame. Text stays legible to the model well
#: below native resolution, and every pixel is paid for on the uplink.
MAX_EDGE_PX = 1024

#: JPEG quality. 55 keeps UI text readable while roughly halving the payload
#: against the usual default of 75.
JPEG_QUALITY = 55

#: Mean absolute difference, on a 0-255 greyscale thumbnail, above which the
#: screen counts as changed. Tuned to ignore a blinking cursor or a clock tick
#: while catching a window switch or a new dialog.
CHANGE_THRESHOLD = 2.0

#: Thumbnail edge used for that comparison. Small on purpose: this runs on a
#: timer forever, so it has to be nearly free.
DIFF_EDGE_PX = 64


class ScreenShare:
    """Samples the screen and hands frames to a sink, while switched on.

    The sink is called from this thread with (jpeg_bytes, mime_type); it is
    expected to be cheap and non-blocking, because falling behind here means
    falling behind on audio too.
    """

    def __init__(self, sink: Callable[[bytes, str], None],
                 log: Callable[..., None] | None = None):
        self._sink = sink
        self._log = log or (lambda *a: None)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._enabled = False
        self._last_thumb: np.ndarray | None = None
        self._last_sent_at = 0.0
        self.frames_sent = 0
        self.frames_skipped = 0
        self.bytes_sent = 0
        self.last_error = ""

    # ── availability ──────────────────────────────────────────────────────

    @staticmethod
    def available() -> bool:
        return HAS_MSS and HAS_PIL

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> dict:
        with self._lock:
            if self._enabled:
                return {"ok": True, "message": "already watching"}
            if not self.available():
                missing = "mss" if not HAS_MSS else "Pillow"
                self.last_error = f"{missing} is not installed"
                return {"ok": False, "reason": self.last_error}
            self._enabled = True
            self._last_thumb = None
            self._last_sent_at = 0.0
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="nova-screen",
                                        daemon=True)
        self._thread.start()
        self._log("[SCREEN] watching the screen (max 1 frame / %.0fs)",
                  MIN_INTERVAL_S)
        return {"ok": True, "message": "watching"}

    def stop(self) -> dict:
        with self._lock:
            was = self._enabled
            self._enabled = False
        self._stop.set()
        t = self._thread
        self._thread = None
        if t is not None:
            t.join(timeout=2.0)
        # Hold nothing once we are no longer looking.
        self._last_thumb = None
        if was:
            self._log("[SCREEN] stopped watching (%d frames, %.0f kB)",
                      self.frames_sent, self.bytes_sent / 1024)
        return {"ok": True, "message": "stopped"}

    def status(self) -> dict:
        with self._lock:
            enabled = self._enabled
        return {
            "available": self.available(),
            "watching": enabled,
            "frames_sent": self.frames_sent,
            "frames_skipped": self.frames_skipped,
            "kb_sent": round(self.bytes_sent / 1024, 1),
            "min_interval_s": MIN_INTERVAL_S,
            "error": self.last_error,
        }

    # ── the sampler ───────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            with mss.mss() as sct:
                monitor = sct.monitors[0]      # the whole virtual desktop
                while not self._stop.wait(MIN_INTERVAL_S):
                    if not self.enabled:
                        break
                    try:
                        self._tick(sct, monitor)
                    except Exception as e:
                        self.last_error = str(e)
                        self._log("[SCREEN] capture failed: %s", e)
                        # A transient capture failure (a lock screen, a
                        # fullscreen game, a display change) is not a reason
                        # to stop watching for the rest of the session.
                        self._stop.wait(2.0)
        except Exception as e:
            self.last_error = str(e)
            self._log("[SCREEN] capture unavailable: %s", e)
            with self._lock:
                self._enabled = False

    def _tick(self, sct: Any, monitor: dict) -> None:
        raw = sct.grab(monitor)
        img = Image.frombytes("RGB", raw.size, raw.rgb)

        thumb = np.asarray(
            img.convert("L").resize((DIFF_EDGE_PX, DIFF_EDGE_PX)),
            dtype=np.float32)
        changed = True
        if self._last_thumb is not None:
            changed = float(np.abs(thumb - self._last_thumb).mean()) > CHANGE_THRESHOLD
        stale = (time.time() - self._last_sent_at) > MAX_INTERVAL_S
        if not (changed or stale):
            self.frames_skipped += 1
            return
        self._last_thumb = thumb

        w, h = img.size
        scale = min(1.0, MAX_EDGE_PX / max(w, h))
        if scale < 1.0:
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                             Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        payload = buf.getvalue()

        self._sink(payload, "image/jpeg")
        self.frames_sent += 1
        self.bytes_sent += len(payload)
        self._last_sent_at = time.time()


__all__ = ["ScreenShare", "MIN_INTERVAL_S", "MAX_INTERVAL_S", "MAX_EDGE_PX",
           "JPEG_QUALITY", "CHANGE_THRESHOLD"]
