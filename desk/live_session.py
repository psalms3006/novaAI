"""desk.live_session — server-side Gemini Live session for native audio.

Bridges Gemini Live (google-genai 2.17.0+client.aio.live.connect) to the SPA
via a local WebSocket.  Mic capture stays server-side (sounddevice) to avoid
WebView2 permission quirks.  Audio output is forwarded as raw 24 kHz mono
PCM 16-bit chunks over JSON text frames — the SPA schedules them on a Web
Audio AudioContext for gapless playback with instant barge-in cancellation.

Honest failure reporting: if the Gemini key is missing or the session cannot
connect, start() returns ok:false with a reason string — the SPA falls back
to the existing push-to-talk flow without pretending.
"""
from __future__ import annotations

import asyncio
import base64
import os
import queue
import threading
import time
import uuid
from enum import Enum
from typing import Any

import numpy as np

import nova_voice
from desk.screen_share import ScreenShare

try:
    import sounddevice as sd
    HAS_SD = True
except Exception:
    sd = None
    HAS_SD = False

try:
    from google import genai
    from google.genai import types as gtypes
    HAS_GEMINI = True
except Exception:
    genai = None
    gtypes = None
    HAS_GEMINI = False

LIVE_MODEL_DEFAULT = "models/gemini-3.1-flash-live-preview"
MIC_RATE = 16000

#: Mic block size. 1024 frames at 16 kHz is 64 ms — the same granularity the
#: reference implementation uses, and small enough that a barge-in is noticed
#: promptly without waking the callback so often that it costs measurable CPU.
MIC_BLOCK = 1024

#: Mic frames buffered before the oldest is dropped. Two seconds. Anything
#: longer is not a buffer, it is a delay: audio that stale has already lost
#: the conversation it belonged to.
MIC_QUEUE_FRAMES = 32

#: Audio gathered into one outbound message.
#:
#: Frames are captured at 64 ms because that is the granularity barge-in
#: detection needs, but transmitting at 64 ms means 15.6 WebSocket messages a
#: second, and the socket is where this pipeline actually falls over. Measured
#: against Gemini Live from this machine over 100 s at a constant bit rate:
#:
#:     64 ms messages (15.6/s):  6 sends stalled past 250 ms, worst 2.9 s
#:    200 ms messages ( 5.0/s):  2 sends stalled past 250 ms, worst 3.0 s
#:    500 ms messages ( 2.0/s):  5 sends stalled past 250 ms, worst 14.9 s
#:
#: The cost is per message, not per byte, so batching helps — but only up to a
#: point, past which each message is large enough to stall on its own. 200 ms
#: is the measured floor, and it is well inside what turn detection needs.
#:
#: Expressed as exactly three 64 ms frames so a batch is never a frame and a
#: bit, which would round up to 256 ms and quietly cost latency nobody chose.
SEND_BATCH_MS = 192

#: While NOVA is speaking the microphone is muted, so every batch is digital
#: silence. Sending 5 messages a second of zeroes buys nothing and competes
#: with the audio coming back for the same congested uplink. One every 800 ms
#: is enough to keep the stream from looking abandoned.
SILENCE_KEEPALIVE_MS = 800
# If 'speaking' is set the mic is muted, so a stuck flag means NOVA is deaf.
# Longest plausible gap between audio chunks inside one model turn.
SPEAKING_WATCHDOG_S = 3.0

#: How long NOVA will wait for Core to produce something worth saying before
#: falling back to a plain greeting. The user is already waiting to talk.
GREETING_BUDGET_S = 6.0

#: Microphone audio that must have reached the model before NOVA is allowed
#: to send anything else. One second at 16 kHz mono 16-bit. See
#: LiveManager._await_mic_stream for why this is not optional.
MIC_WARMUP_BYTES = MIC_RATE * 2

#: ...and how long to wait for it before giving up. A microphone that never
#: produces anything is a broken microphone, not a reason to stay silent.
MIC_WARMUP_WAIT_S = 4.0

#: How long the greeting waits for an interface to attach before going ahead.
#:
#: Short, because it is a courtesy rather than the readiness check. What
#: actually has to be true before NOVA speaks is that the session is
#: connected, the speaker is open, the microphone is open and real audio has
#: reached the model — all of which are checked separately. Waiting eight
#: seconds on a surface that may never attach (a headless run, a window that
#: reloaded during boot) just delays the greeting for no benefit; it was
#: observed doing exactly that.
SURFACE_WAIT_S = 2.5

#: How long a tool may run before NOVA stops waiting for it. The model is
#: blocked on the response for this whole time, and so is the conversation, so
#: it has to be long enough for a web search and short enough that a wedged
#: tool does not take the session with it.
TOOL_TIMEOUT_S = 30.0

#: Longest a screen frame may go unrefreshed inside one conversational turn.
#: Within a turn the screen rarely matters more than once; across turns a
#: fresh frame is sent as soon as one is captured.
SCREEN_REFRESH_S = 10.0

#: Output buffer depth requested from the sound device, in seconds. See
#: _start_playback for the measurements behind it.
OUTPUT_LATENCY_S = 0.5

#: Audio held back before playback starts, per turn.
#:
#: Lower than it was (220 ms), because the device now holds half a second of
#: cushion itself and there is no reason to pay for the same insurance twice.
#: This only has to cover the gap between the first chunk and the second.
PREROLL_MS = 120
LIVE_RATE = 24000  # Gemini Live outputs 24 kHz 16-bit mono PCM


class VoiceTrace:
    """Stage timings for one voice session.

    Deliberately not per-chunk: a line for every 20 ms of audio buries the
    events that matter. This records the boundaries -- when the mic became
    ready, when the user's speech reached the model, when audio came back,
    when it was audible -- and reports the gaps between them, which is what
    "why was that slow" actually needs.
    """

    __slots__ = ("session_id", "t0", "stages", "_lock", "_turn", "_turn_t0")

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.t0 = time.time()
        self.stages: dict[str, float] = {}
        self._lock = threading.Lock()
        self._turn = 0
        self._turn_t0 = 0.0

    def mark(self, stage: str, **detail) -> float:
        """Record a stage and return milliseconds since the session began."""
        now = time.time()
        ms = (now - self.t0) * 1000
        with self._lock:
            first = stage not in self.stages
            if first:
                self.stages[stage] = now
        extra = "  ".join(f"{k}={v}" for k, v in detail.items())
        _log("[VOICE %s] %-22s +%7.0f ms  %s", self.session_id, stage, ms, extra)
        return ms

    def turn_started(self) -> None:
        with self._lock:
            self._turn += 1
            self._turn_t0 = time.time()
        _log("[VOICE %s] turn %d: user speech detected", self.session_id, self._turn)

    def turn_timing(self, label: str) -> None:
        """Latency within the current turn, which is what the user feels."""
        if not self._turn_t0:
            return
        _log("[VOICE %s] turn %d: %-18s +%6.0f ms since speech",
             self.session_id, self._turn, label,
             (time.time() - self._turn_t0) * 1000)

    def gap(self, a: str, b: str) -> float | None:
        with self._lock:
            if a in self.stages and b in self.stages:
                return (self.stages[b] - self.stages[a]) * 1000
        return None

    def summary(self) -> dict:
        pairs = (("connect_start", "connected"),
                 ("connected", "mic_ready"),
                 ("mic_ready", "first_user_audio"),
                 ("first_user_audio", "first_model_audio"),
                 ("first_model_audio", "playback_started"))
        return {f"{a}->{b}": round(v, 1)
                for a, b in pairs if (v := self.gap(a, b)) is not None}


def _compose_opening_line() -> str:
    """Ask NOVA Core whether it has anything worth saying. Runs in a thread."""
    try:
        import nova_core_voice
        return nova_core_voice.opening_line(_load_meta()) or ""
    except Exception:
        return ""


#: Words below which a turn is not worth considering for memory. "yes",
#: "thanks", "stop" carry nothing forward.
MEMORY_MIN_WORDS = 4

#: Never hand these to the extractor, whatever else is true of the turn.
#: Credentials do not become less sensitive for having been said out loud, and
#: an assistant that files them away under "useful context" is a liability.
MEMORY_FORBIDDEN = (
    "password", "passphrase", "api key", "api-key", "secret key", "token is",
    "credit card", "card number", "cvv", "social security", "ssn",
    "bank account", "routing number", "pin is", "private key", "seed phrase",
)


def _remember_turn(user_text: str, nova_text: str) -> None:
    """Consider one finished turn for long-term memory. Runs in a thread.

    NOVA Core already knows how to decide what is worth keeping and how to
    store it — this only feeds it, which the desktop session never did. That
    is why nothing said to the desktop app survived a restart while the
    terminal remembered perfectly well.
    """
    user_text = (user_text or "").strip()
    nova_text = (nova_text or "").strip()
    if len(user_text.split()) < MEMORY_MIN_WORDS:
        return
    haystack = f"{user_text} {nova_text}".lower()
    if any(bad in haystack for bad in MEMORY_FORBIDDEN):
        _log("[LIVE] turn withheld from memory: looks like a credential")
        return
    try:
        from memory_extra import extract_memory_updates, load_memory
        extract_memory_updates(user_text, nova_text, load_memory())
    except Exception as e:
        _log("[LIVE] memory extraction unavailable: %s", e)


def _telemetry(event: str, **attrs) -> None:
    """Operational telemetry. Metadata only -- no transcripts, no audio, ever.

    Wrapped so a telemetry problem can never affect the voice pipeline.
    """
    try:
        import nova_account
        nova_account.emit(event, **{k: v for k, v in attrs.items() if v is not None})
    except Exception:
        pass


#: WebSocket keepalive, widened from the library default of 20 s.
#:
#: The client sends a ping every interval and closes the connection with 1011
#: if no pong comes back within the timeout. On a congested uplink a single
#: outbound send was measured stalling for up to 15 s, and the pong sits
#: behind that stall in the same socket — so the default closed a session
#: that was about to recover perfectly well. Every failure in the reported
#: logs was this: "sent 1011 (internal error) keepalive ping timeout", roughly
#: 70 seconds in, mid-conversation, over and over.
#:
#: Pinging is kept, at the same rate: a genuinely dead socket must still be
#: noticed. Only the patience for an answer changes.
WS_PING_INTERVAL_S = 20
WS_PING_TIMEOUT_S = 75


def _relax_websocket_keepalive(client: Any) -> bool:
    """Widen the Live socket's keepalive timeout, if the SDK still allows it.

    google-genai splats `_api_client._websocket_ssl_ctx` straight into
    `websockets.connect()`, so extra keyword arguments placed there reach the
    library. That is a private attribute and it may not survive an SDK
    upgrade, which is why this reports failure rather than raising: a session
    with the default keepalive is worse, not broken.
    """
    try:
        kwargs = client._api_client._websocket_ssl_ctx
        if not isinstance(kwargs, dict):
            raise TypeError(type(kwargs).__name__)
        kwargs["ping_interval"] = WS_PING_INTERVAL_S
        kwargs["ping_timeout"] = WS_PING_TIMEOUT_S
        return True
    except Exception as e:
        _log("[LIVE] could not widen WebSocket keepalive (%s); using the "
             "library default, so a long network stall may drop the session", e)
        return False


class MicBatcher:
    """Gathers 64 ms mic frames into messages worth sending.

    Two jobs. It batches frames up to :data:`SEND_BATCH_MS`, because the
    per-message cost on the socket is what limits this pipeline rather than
    the bitrate. And it throttles runs of pure silence — while NOVA is talking
    the gate mutes the microphone, and transmitting that at full rate spends
    upstream bandwidth on zeroes at exactly the moment her audio needs it.
    """

    def __init__(self, rate: int = MIC_RATE,
                 batch_ms: int = SEND_BATCH_MS,
                 silence_ms: int = SILENCE_KEEPALIVE_MS):
        self._target = int(rate * batch_ms / 1000) * 2      # bytes, int16
        self._silence_gap = silence_ms / 1000.0
        self._buf: list[bytes] = []
        self._bytes = 0
        self._all_silence = True
        self._last_silence_sent = 0.0

    def add(self, payload: bytes, silent: bool) -> bytes | None:
        """Returns a message to send, or None while still gathering."""
        self._buf.append(payload)
        self._bytes += len(payload)
        if not silent:
            self._all_silence = False
        if self._bytes < self._target:
            return None

        batch = b"".join(self._buf)
        silence = self._all_silence
        self._buf.clear()
        self._bytes = 0
        self._all_silence = True

        now = time.monotonic()
        if silence:
            if now - self._last_silence_sent < self._silence_gap:
                return None
            self._last_silence_sent = now
        else:
            # Real audio resets the clock: the next quiet stretch gets its full
            # keepalive interval rather than one measured from ancient history.
            self._last_silence_sent = now
        return batch

    def reset(self) -> None:
        self._buf.clear()
        self._bytes = 0
        self._all_silence = True
        self._last_silence_sent = 0.0


class LiveState(str, Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    STREAMING = "streaming"
    DISCONNECTING = "disconnecting"
    ERROR = "error"
    CLOSED = "closed"


class LiveEvent:
    __slots__ = ("type", "ts", "data")

    def __init__(self, event_type: str, **kw: Any):
        self.type = event_type
        self.ts = time.time()
        self.data = kw

    def to_dict(self) -> dict:
        d = {"type": self.type, "ts": self.ts}
        d.update(self.data)
        return d


def _log(msg: str, *a: Any) -> None:
    try:
        import nova as _nova
        _nova.log.info(msg, *a)
    except Exception:
        print(msg % a if a else msg)


def _resolve(name: str, default: Any = None) -> Any:
    try:
        import nova as _nova
        return getattr(_nova, name, default)
    except Exception:
        return default


def _load_meta() -> dict:
    try:
        from memory_extra import load_memory
        return load_memory()
    except Exception:
        return {}


def _build_memory_context(meta: dict) -> str:
    try:
        from memory_extra import build_memory_context
        return build_memory_context(meta)
    except Exception:
        try:
            from live_extra import build_memory_context
            return build_memory_context(meta)
        except Exception:
            return ""


# ── LiveManager ──────────────────────────────────────────────────────────────


class LiveManager:
    """Persistent Gemini Live session in a background asyncio thread.

    Lifecycle:
        start() -> state CONNECTING -> CONNECTED/STREAMING -> stop()

    Audio:
        - Mic captured server-side via sounddevice, forwarded to Live
        - Live responses arrive as base64-encoded PCM 24 kHz chunks
        - Published as LiveEvent("audio", data_b64=...) for SPA playback

    Events (via subscribe() -> queue.Queue[LiveEvent]):
        state, audio, user_transcript, nova_transcript,
        turn_complete, interrupted, error, go_away
    """

    # Reconnect policy for a dropped/failed Live socket.
    MAX_RECONNECT_ATTEMPTS = 6
    RECONNECT_BASE_DELAY = 2.0
    RECONNECT_MAX_DELAY = 60.0

    #: How often to look for the network while offline. Cheap — a failed DNS
    #: lookup costs nothing — and short enough that voice returns on its own
    #: shortly after the connection does.
    OFFLINE_RETRY_DELAY = 5.0

    def __init__(self, model: str | None = None, voice: str | None = None):
        self._model = model or os.getenv("LIVE_MODEL", LIVE_MODEL_DEFAULT)
        self._voice = voice or os.getenv("NOVA_VOICE", _resolve("NOVA_VOICE", "Aoede"))
        self._state = LiveState.IDLE
        self._state_lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self._subs_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: Any = None
        self._mic_active = False
        self._mic_stream: Any = None
        # Mic frames go straight from the PortAudio callback onto the event
        # loop, the way the reference implementation does it.
        #
        # The previous pump was a threading.Queue drained by
        # loop.run_in_executor(queue.get, timeout=0.1). That polls a worker
        # thread 15 times a second forever, and when a send ran long the queue
        # backed up and was then flushed as a burst — 106 frames dropped in a
        # 100 second session, and the outbound burst delayed the WebSocket
        # keepalive behind it until the socket was closed with 1011. An
        # asyncio.Queue posted from the callback has no polling, no worker
        # thread and no burst: one frame in, one frame out.
        self._mic_queue: asyncio.Queue | None = None
        self._input_text_queue: asyncio.Queue | None = None
        self._t_first_audio: float = 0.0
        self._t_connected: float = 0.0
        self._start_time: float = 0.0
        self._last_error: str = ""
        self._turn_count: int = 0
        self._audio_bytes_out: int = 0
        self._audio_bytes_in: int = 0
        self._has_greeted: bool = False

        # One shared voice policy — identical rules to the terminal path.
        self._gate = nova_voice.VoiceGate(
            chunk_samples=MIC_BLOCK, on_barge_in=self._barge_in,
        )
        # NOVA Core owns playback. The UI surfaces receive amplitude/state and
        # only visualise it, so the fullscreen window and the ambient orb can
        # never play the same audio twice.
        self._play_q: queue.Queue = queue.Queue(maxsize=200)
        self._play_thread: threading.Thread | None = None
        self._play_stop = threading.Event()
        self._turn_done_flag = False
        self._speaker_alive = False
        self._last_audio_at = 0.0
        self._t_mic_ready = 0.0
        self._mic_dropped = 0
        self._send_total = 0.0
        self._send_count = 0
        self._send_worst = 0.0
        self._last_drop_log = 0.0
        self._batcher = MicBatcher()
        #: The output stream, and when the audio already handed to it will
        #: have finished playing (time.monotonic clock). Guarded because
        #: barge-in aborts the device from the microphone callback thread
        #: while the playback thread may be writing to it.
        self._stream: Any = None
        self._stream_lock = threading.Lock()
        self._playing_until = 0.0
        # Bumped whenever playback is cut. The pre-roll lives inside the
        # speaker thread, so draining _play_q alone would leave held audio to
        # play after an interruption -- exactly the "speaks a fragment of the
        # old response" behaviour barge-in exists to prevent.
        self._play_generation = 0
        self._trace: VoiceTrace | None = None
        self._auth_rejected = False
        self._offline = False
        # Screen awareness shares this session rather than opening its own.
        # Ambient mode is a second view of one NOVA, so what she can see has
        # to arrive in the same conversation she is already having.
        self._screen = ScreenShare(sink=self._send_screen_frame, log=_log)
        self._video_queue: asyncio.Queue | None = None

    # ── public API ────────────────────────────────────────────────────────

    def start(self) -> dict:
        with self._state_lock:
            if self._state in (LiveState.CONNECTING, LiveState.CONNECTED, LiveState.STREAMING):
                return {"ok": True, "message": "already running"}
            if not HAS_GEMINI:
                return {"ok": False, "reason": "google-genai SDK not installed"}
            key = os.environ.get("GEMINI_API_KEY", "").strip()
            if not key:
                return {"ok": False, "reason": "no Gemini API key available"}
            self._state = LiveState.CONNECTING
            self._last_error = ""
            self._t_first_audio = 0.0
            self._t_connected = 0.0
            self._start_time = time.time()
            self._turn_count = 0
            self._audio_bytes_out = 0
            self._audio_bytes_in = 0
            self._has_greeted = False
            self._auth_rejected = False
            self._offline = False
        self._trace = VoiceTrace(uuid.uuid4().hex[:6])
        self._trace.mark("connect_start")
        self._thread = threading.Thread(target=self._run_loop, name="nova-live", daemon=True)
        self._thread.start()
        _telemetry("VOICE_SESSION_STARTED", surface="desktop")
        return {"ok": True, "message": "connecting"}

    def stop(self) -> dict:
        with self._state_lock:
            if self._state in (LiveState.IDLE, LiveState.CLOSED):
                return {"ok": True, "message": "not running"}
            self._state = LiveState.DISCONNECTING
        self._publish(LiveEvent("state", state="disconnecting"))
        # Sentinels unblock the senders; both queues belong to the loop, so
        # they have to be posted from it.
        self._screen.stop()
        self._post_to_loop(self._mic_queue, None)
        self._post_to_loop(self._input_text_queue, None)
        self._post_to_loop(self._video_queue, None)
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=3.0)
        self._stop_mic()
        self._session = None
        with self._state_lock:
            self._state = LiveState.CLOSED
        self._publish(LiveEvent("state", state="closed"))
        _telemetry("VOICE_SESSION_ENDED", surface="desktop",
                   duration_ms=int((time.time() - self._start_time) * 1000)
                   if self._start_time else None,
                   count=self._turn_count)
        return {"ok": True, "message": "stopped"}

    def send_text(self, text: str) -> dict:
        with self._state_lock:
            if self._state not in (LiveState.CONNECTED, LiveState.STREAMING):
                return {"ok": False, "reason": "not connected"}
        if not self._post_to_loop(self._input_text_queue, text):
            return {"ok": False, "reason": "session loop not running"}
        return {"ok": True}

    def _post_to_loop(self, q: "asyncio.Queue | None", item: Any) -> bool:
        """Put an item on a loop-owned queue from any thread."""
        loop = self._loop
        if q is None or loop is None or loop.is_closed():
            return False
        try:
            loop.call_soon_threadsafe(q.put_nowait, item)
            return True
        except (RuntimeError, asyncio.QueueFull):
            return False

    def set_screen_share(self, enabled: bool) -> dict:
        """Turn screen awareness on or off for the running session."""
        if enabled:
            with self._state_lock:
                if self._state not in (LiveState.CONNECTED, LiveState.STREAMING):
                    return {"ok": False, "watching": False,
                            "reason": "voice session not running"}
            result = self._screen.start()
        else:
            result = self._screen.stop()
        self._publish(LiveEvent("screen_share", watching=self._screen.enabled,
                                **({"reason": result["reason"]}
                                   if not result.get("ok") else {})))
        return {**result, "watching": self._screen.enabled}

    def screen_status(self) -> dict:
        return self._screen.status()

    def _send_screen_frame(self, jpeg: bytes, mime: str) -> None:
        """Hand a captured frame to the session. Called from the sampler.

        Dropped rather than queued if the session is busy: a stale screenshot
        is worth less than the audio it would delay, and another frame is two
        seconds away.
        """
        q = self._video_queue
        loop = self._loop
        if q is None or loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._offer_video, q, jpeg, mime)
        except RuntimeError:
            pass

    @staticmethod
    def _offer_video(q: "asyncio.Queue", jpeg: bytes, mime: str) -> None:
        try:
            q.put_nowait((jpeg, mime))
        except asyncio.QueueFull:
            pass

    def status(self) -> dict:
        with self._state_lock:
            state = self._state.value
        t_first = 0.0
        if self._t_first_audio and self._t_connected:
            t_first = round((self._t_first_audio - self._t_connected) * 1000)
        uptime = 0.0
        if self._start_time and state not in ("idle",):
            uptime = round(time.time() - self._start_time, 1)
        return {
            "ok": True,
            "state": state,
            "model": self._model,
            "voice": self._voice,
            "native_audio": True,
            "engine": "gemini-live",
            "t_first_audio_ms": t_first,
            "uptime_s": uptime,
            "turns": self._turn_count,
            "audio_out_kb": round(self._audio_bytes_out / 1024, 1),
            "audio_in_kb": round(self._audio_bytes_in / 1024, 1),
            "screen": self._screen.status(),
            "error": self._last_error,
        }

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._subs_lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._subs_lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    # ── mic ───────────────────────────────────────────────────────────────

    # ── playback (Core-owned) ─────────────────────────────────────────────

    def _start_playback(self) -> None:
        """Open the speaker, then start draining the queue into it.

        The device is opened *synchronously*, before this returns. It used to
        be opened inside the worker thread, which meant the session declared
        itself ready and sent the greeting while the speaker was still
        opening; the first chunks of NOVA's first sentence arrived to find
        `_speaker_alive` still false and were dropped on the floor as
        "speaker_unavailable". A greeting that starts mid-word is not a
        greeting.
        """
        if self._play_thread is not None or not HAS_SD:
            return False
        self._play_stop.clear()
        try:
            # A deliberately deep output buffer. This is the cracking.
            #
            # An empty output buffer is a click, and model audio arrives in
            # bursts over a network that was measured stalling for seconds at
            # a time. So the only question that matters is how long a gap the
            # device can absorb before it runs dry. Measured on this machine,
            # writing Gemini-sized chunks:
            #
            #   default latency   182 ms of buffer
            #   latency="high"    182 ms  (no different here)
            #   latency=0.5       504 ms
            #   latency=1.0      1000 ms
            #
            # "high" is a hint the device is free to ignore, and this one does.
            # An explicit half second nearly triples the cushion for a delay
            # the user cannot perceive at the start of a turn — and barge-in
            # discards the buffer outright with abort(), so a deeper buffer
            # costs nothing when she is interrupted.
            stream = sd.RawOutputStream(
                samplerate=nova_voice.RECEIVE_RATE,
                channels=nova_voice.CHANNELS,
                dtype="int16",
                blocksize=0,
                latency=OUTPUT_LATENCY_S,
            )
            stream.start()
            self._stream = stream
        except Exception as e:
            _log("[LIVE] speaker open failed: %s", e)
            # Make sure a dead speaker never leaves the mic muted.
            self._speaker_alive = False
            self._gate.set_speaking(False)
            self._publish(LiveEvent(
                "error", error="speaker_unavailable",
                message=("NOVA could not open your speakers (%s), so you will "
                         "not hear her replies." % type(e).__name__)))
            return False

        self._speaker_alive = True
        _log("[LIVE] speaker ready (%d Hz)", nova_voice.RECEIVE_RATE)

        def _worker():
            # Pre-roll: hold a little audio back before the first write.
            #
            # Starting playback on the very first chunk means the device is
            # consuming in real time while the network is still delivering the
            # rest of the sentence. One late packet is then an audible gap.
            # Buffering PREROLL_MS first costs that much added latency once,
            # at the start of a turn, and buys a cushion for the whole turn.
            preroll: list[bytes] = []
            preroll_bytes = 0
            need = int(nova_voice.RECEIVE_RATE * PREROLL_MS / 1000) * 2
            primed = False
            generation = self._play_generation

            def emit(chunk: bytes) -> None:
                # Tell the gate what the room is about to hear, at the moment
                # it hears it. The echo canceller aligns against this, so
                # handing it audio at enqueue time instead — seconds before
                # the device plays it — would put the reference outside the
                # search window and cancel nothing.
                self._gate.reference(chunk, nova_voice.RECEIVE_RATE)
                # Account for what the device now owes the room.
                #
                # write() returns as soon as the buffer accepts the audio, not
                # when it is heard. Tracking the difference is the only way to
                # know when NOVA has actually stopped talking — everything
                # else here treats "the queue is empty" as "she finished",
                # which unmutes the microphone into the middle of her own
                # sentence.
                duration = len(chunk) / 2 / nova_voice.RECEIVE_RATE
                now = time.monotonic()
                self._playing_until = max(self._playing_until, now) + duration
                with self._stream_lock:
                    stream.write(chunk)

            try:
                while not self._play_stop.is_set():
                    try:
                        chunk = self._play_q.get(timeout=0.2)
                    except queue.Empty:
                        if self._play_generation != generation:
                            generation = self._play_generation
                            preroll.clear(); preroll_bytes = 0; primed = False
                            continue
                        # A pause with audio still held back means the turn is
                        # short: play what there is rather than waiting for a
                        # cushion that will never fill.
                        if preroll and not primed:
                            for held in preroll:
                                try:
                                    emit(held)
                                except Exception:
                                    break
                            preroll.clear(); preroll_bytes = 0; primed = True
                        # Queue drained, the model finished its turn, and the
                        # sound device has actually played it all out. Only
                        # then has NOVA stopped talking.
                        if (self._gate.speaking and self._turn_done_flag
                                and self._playback_drained()):
                            self._finish_speaking()
                            primed = False          # next turn re-buffers
                        elif self._gate.speaking and self._last_audio_at:
                            # Watchdog. 'speaking' mutes the mic, so anything
                            # that leaves it set is deafness. If the queue has
                            # been empty well past any plausible gap in the
                            # model's audio, stop believing NOVA is talking.
                            if (time.time() - self._last_audio_at > SPEAKING_WATCHDOG_S
                                    and self._playback_drained()):
                                _log("[LIVE] watchdog: clearing stuck speaking state")
                                self._finish_speaking()
                                primed = False
                        continue
                    if chunk is None:
                        break
                    if self._play_generation != generation:
                        # Playback was cut while this chunk was in flight.
                        generation = self._play_generation
                        preroll.clear(); preroll_bytes = 0; primed = False
                        continue
                    try:
                        if not primed:
                            preroll.append(chunk)
                            preroll_bytes += len(chunk)
                            if preroll_bytes >= need:
                                for held in preroll:
                                    emit(held)
                                preroll.clear(); preroll_bytes = 0
                                primed = True
                                if self._trace:
                                    self._trace.mark("playback_started")
                                    self._trace.turn_timing("playback started")
                        else:
                            emit(chunk)
                    except Exception as e:
                        _log("[LIVE] speaker write failed: %s", e)
            finally:
                self._speaker_alive = False
                self._gate.set_speaking(False)
                self._stream = None
                try:
                    # stop(), not abort(): on the way out, let whatever is
                    # already in the device finish rather than clipping the
                    # last word of a goodbye.
                    stream.stop(); stream.close()
                except Exception:
                    pass

        self._play_thread = threading.Thread(target=_worker, name="nova-speaker",
                                             daemon=True)
        self._play_thread.start()
        return True

    def _playback_drained(self) -> bool:
        """Has the sound device finished playing everything it was given?

        The model running out of chunks is not NOVA finishing her sentence.
        There is up to a second of audio sitting in the device after the last
        write, and treating the two as the same event unmuted the microphone
        while she was still audibly talking — so she heard her own voice, the
        model received it as the user speaking, and the turn fell apart.

        MODEL_RESPONSE_COMPLETE and AUDIO_PLAYBACK_COMPLETE are separate
        things. This is the second one.
        """
        return time.monotonic() >= self._playing_until

    def _finish_speaking(self) -> None:
        """NOVA has stopped talking: reopen the mic and say so."""
        self._gate.set_speaking(False)
        self._turn_done_flag = False
        self._playing_until = 0.0
        self._publish(LiveEvent("playback_complete"))
        self._publish(LiveEvent("state", state="listening"))

    def _stop_playback(self) -> None:
        self._play_stop.set()
        try:
            self._play_q.put_nowait(None)
        except Exception:
            pass
        self._play_thread = None

    def _enqueue_audio(self, audio: bytes) -> None:
        """Play a chunk and tell the surfaces how loud it is.

        There is deliberately no post-interruption suppression here. There
        used to be: an epoch latch that discarded every chunk of a turn once
        it had been interrupted. It existed because the barge-in detector was
        firing on NOVA's own echo, so playback had to be kept from resuming
        and re-triggering it — and when the latch failed to clear, NOVA went
        silent for the rest of the session while the model went on replying.
        With echo cancellation the premise is gone: an interruption now means
        the user really spoke, the model is told, and it stops on its own.
        """
        if not self._speaker_alive:
            # No playback thread: never mute the mic on its behalf, or NOVA
            # would be permanently deaf on a machine with no working speaker.
            self._publish(LiveEvent("error", error="speaker_unavailable"))
            return
        self._last_audio_at = time.time()
        self._gate.set_speaking(True)
        try:
            self._play_q.put_nowait(audio)
        except queue.Full:
            pass
        # Amplitude drives the orb on every surface. Cheap: one pass over a
        # decimated view, not the whole buffer.
        try:
            samples = np.frombuffer(audio, dtype=np.int16)[::16]
            level = float(np.abs(samples).max()) / 32768.0 if samples.size else 0.0
        except Exception:
            level = 0.0
        self._publish(LiveEvent("audio_level", level=round(level, 4)))

    def _barge_in(self) -> None:
        """User spoke over NOVA — stop playing and let them finish."""
        # Clear speaking here too, not just in VoiceGate.process. "Stop
        # talking" has to be self-sufficient: any caller of _barge_in should
        # leave NOVA genuinely stopped, not rely on the detector having
        # already done half the work.
        # interrupted=True: the user is mid-sentence, so the speaker-tail
        # cooldown must not swallow the next quarter second of it.
        self._gate.set_speaking(False, interrupted=True)
        self._turn_done_flag = False
        self._play_generation += 1        # discard anything held in the pre-roll
        dropped = nova_voice.drain(self._play_q)
        discarded_ms = self._abort_playback()
        _log("[LIVE] barge-in — dropped %d queued chunks, %d ms already in "
             "the sound device", dropped, discarded_ms)
        self._publish(LiveEvent("playback_cancelled", queued_chunks=dropped,
                                device_ms=discarded_ms))
        self._publish(LiveEvent("interrupted", reason="barge_in"))
        self._publish(LiveEvent("state", state="listening"))

    def _abort_playback(self) -> int:
        """Throw away audio the sound device has already accepted.

        Draining our own queue is not stopping. Everything handed to
        `stream.write()` lives in the device buffer, which on WASAPI is
        several hundred milliseconds deep and on some devices far more — so
        NOVA calmly finished her sentence out of it while the interface said
        "listening". That is the reported barge-in failure: the user talks
        over her, and she talks on regardless.

        PortAudio's `abort()` discards those buffers immediately, where
        `stop()` politely waits for them to finish playing. Returns roughly
        how much audio was thrown away, for the log.
        """
        stream = self._stream
        if stream is None:
            return 0
        pending_ms = max(0, int((self._playing_until - time.monotonic()) * 1000))
        with self._stream_lock:
            try:
                stream.abort()
                stream.start()          # abort leaves it stopped
            except Exception as e:
                _log("[LIVE] could not abort playback: %s", e)
                return 0
        self._playing_until = 0.0
        return pending_ms

    def set_muted(self, value: bool) -> dict:
        self._gate.set_muted(bool(value))
        self._publish(LiveEvent("state", state="muted" if value else "listening"))
        return {"ok": True, "muted": self._gate.muted}

    @property
    def muted(self) -> bool:
        return self._gate.muted

    def _start_mic(self) -> None:
        if self._mic_active:
            return
        if not HAS_SD:
            # Previously this returned in silence. The session went on to
            # connect, the orb went on saying "listening", and NOVA simply
            # could not hear -- with nothing anywhere to say why. An audio
            # stack that failed to load is a hard failure of a voice
            # assistant, so it is reported as one.
            _log("[LIVE] no audio backend: sounddevice failed to import; "
                 "microphone cannot be opened")
            self._publish(LiveEvent(
                "error", error="mic_unavailable",
                message=("NOVA has no audio input backend, so she cannot "
                         "hear you. The sound library failed to load.")))
            self._publish(LiveEvent("state", state="error",
                                    error="mic_unavailable", fatal="audio"))
            return

        loop = self._loop
        q = self._mic_queue

        batcher = self._batcher
        batcher.reset()

        def _cb(indata, frames, time_info, status):
            # Every rule about what reaches the model lives in nova_voice:
            # mute-while-speaking, echo cancellation, barge-in detection.
            try:
                frame = self._gate.process(np.asarray(indata).reshape(-1))
                payload = batcher.add(frame, self._gate.last_was_silence)
            except Exception:
                return
            if payload is None:
                return                      # still gathering, or throttled
            if loop is None or q is None or loop.is_closed():
                return

            def _post():
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    # Drop the OLDEST frame, never the newest.
                    #
                    # This queue is a live conversation, not a recording. When
                    # it backs up, the frames worth keeping are the ones the
                    # user just spoke; discarding those and transmitting
                    # seconds-old audio is how "Hello NOVA" reached the model
                    # detached from its context.
                    try:
                        q.get_nowait()
                        self._mic_dropped += 1
                        # Say so when it starts, not only in the closing
                        # tally. A burst of drops is the user's words going
                        # missing, and knowing *when* it began is the whole
                        # difference between diagnosing it and guessing.
                        now = time.monotonic()
                        if now - self._last_drop_log > 5.0:
                            self._last_drop_log = now
                            _log("[LIVE] mic queue full — dropping frames "
                                 "(%d so far); the sender is not keeping up",
                                 self._mic_dropped)
                        q.put_nowait(payload)
                    except Exception:
                        pass

            try:
                loop.call_soon_threadsafe(_post)
            except RuntimeError:
                pass            # loop shutting down

        try:
            self._mic_stream = sd.InputStream(
                samplerate=MIC_RATE, channels=1, dtype="int16",
                blocksize=MIC_BLOCK, callback=_cb,
            )
            self._mic_stream.start()
            self._mic_active = True
            self._mic_dropped = 0
            _log("[LIVE] mic opened (16 kHz mono, %d-frame blocks, "
                 "%.1fs queue)", MIC_BLOCK,
                 MIC_QUEUE_FRAMES * MIC_BLOCK / MIC_RATE)
        except Exception as e:
            # Same reasoning as above: a device that is missing, busy or
            # blocked by Windows privacy settings must surface, not vanish
            # into a log line nobody reads.
            _log("[LIVE] mic open failed: %s", e)
            self._publish(LiveEvent(
                "error", error="mic_open_failed",
                message=("NOVA could not open your microphone (%s). Check it "
                         "is plugged in and that microphone access is allowed "
                         "for desktop apps in Windows privacy settings."
                         % type(e).__name__)))
            self._publish(LiveEvent("state", state="error",
                                    error="mic_open_failed", fatal="audio"))

    def _stop_mic(self) -> None:
        if self._send_count:
            _log("[LIVE] mic sends: %d, mean %.1f ms, worst %.0f ms",
                 self._send_count,
                 self._send_total / self._send_count * 1000,
                 self._send_worst * 1000)
        if self._mic_dropped:
            # Silence here would hide the pipeline falling behind.
            _log("[LIVE] mic: %d frame(s) dropped this session (consumer "
                 "could not keep up)", self._mic_dropped)
        self._mic_active = False
        if self._mic_stream is not None:
            try:
                self._mic_stream.stop()
                self._mic_stream.close()
            except Exception:
                pass
            self._mic_stream = None
        if self._mic_queue is not None:
            nova_voice.drain(self._mic_queue)

    # ── subscriber fan-out ────────────────────────────────────────────────

    def _publish(self, event: LiveEvent) -> None:
        with self._subs_lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    # ── asyncio loop ──────────────────────────────────────────────────────

    def _should_reconnect(self) -> bool:
        with self._state_lock:
            return self._state not in (LiveState.DISCONNECTING, LiveState.CLOSED)

    def _run_loop(self) -> None:
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._connect_and_run())
        except Exception as e:
            _log("[LIVE] loop died: %s", e)
            self._last_error = str(e)
            with self._state_lock:
                self._state = LiveState.ERROR
            self._publish(LiveEvent("state", state="error", error=str(e)))

    @staticmethod
    def _is_offline(err: str) -> bool:
        """Is this "there is no network" rather than "the service refused us"?

        The distinction decides whether giving up is reasonable. A laptop that
        loses wifi, sleeps, or changes network produces name-resolution
        failures for as long as it is disconnected — and counting those toward
        a six-attempt budget means voice is dead until the application is
        restarted, which is exactly what happened here: six getaddrinfo
        failures in ninety seconds and then a permanent silent give-up.

        Being offline is a state to wait out, not a failure to abandon.
        """
        e = (err or "").lower()
        return ("getaddrinfo failed" in e
                or "name or service not known" in e
                or "temporary failure in name resolution" in e
                or "nodename nor servname" in e
                or "network is unreachable" in e
                or "no route to host" in e
                or "errno 11001" in e
                or "errno 11002" in e)

    @staticmethod
    def _is_auth_failure(err: str) -> bool:
        """Is this a credential problem rather than a network one?

        Gemini Live closes with application code 1007 and "API key not valid"
        when the credential is rejected. Retrying that is pointless: the key
        will still be invalid in two seconds, and in four, and in eight. The
        session log for 2026-09-10 shows exactly that -- six identical
        rejections over 90 seconds, then a silent give-up, while the interface
        went on claiming NOVA was listening.

        Failing fast turns an inscrutable dead microphone into one clear
        sentence the user can act on.
        """
        # Normalise separators: Google returns both "PERMISSION_DENIED" (the
        # status enum) and "permission denied" (prose) for the same condition,
        # and matching only one of them lets the other retry forever.
        e = (err or "").lower().replace("_", " ")
        return ("api key not valid" in e
                or "api key invalid" in e
                or "invalid api key" in e
                or "invalid authentication" in e
                or "unauthenticated" in e
                or "permission denied" in e
                or ("expired" in e and ("token" in e or "credential" in e)))

    async def _connect_and_run(self) -> None:
        meta = _load_meta()
        mem_ctx = _build_memory_context(meta)
        sys_prompt = _resolve("NOVA_SYSTEM_PROMPT", "")
        if mem_ctx:
            sys_prompt += f"\n\n[BACKGROUND MEMORY]\n{mem_ctx}"
        tool_decls = _resolve("TOOL_DECLARATIONS", [])

        try:
            client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
            _relax_websocket_keepalive(client)
        except Exception as e:
            _log("[LIVE] client init failed: %s", e)
            self._last_error = f"client init failed: {e}"
            with self._state_lock:
                self._state = LiveState.ERROR
            self._publish(LiveEvent("state", state="error", error=self._last_error))
            return

        config = gtypes.LiveConnectConfig(
            response_modalities=[gtypes.Modality.AUDIO],
            output_audio_transcription=gtypes.AudioTranscriptionConfig(),
            input_audio_transcription=gtypes.AudioTranscriptionConfig(),
            system_instruction=sys_prompt,
            tools=[{"function_declarations": tool_decls}] if tool_decls else [],
            session_resumption=gtypes.SessionResumptionConfig(),
            speech_config=gtypes.SpeechConfig(
                voice_config=gtypes.VoiceConfig(
                    prebuilt_voice_config=gtypes.PrebuiltVoiceConfig(
                        voice_name=self._voice,
                    )
                )
            ),
        )

        # Reconnect policy. Without a bound, a permanent failure (bad key,
        # revoked quota, broken TLS trust) turned into an infinite 2s retry
        # loop that spammed the log forever and never told the user anything.
        consecutive_failures = 0
        while self._should_reconnect():
            _log("[LIVE] connecting to %s voice=%s ...", self._model, self._voice)
            t0 = time.time()
            connected_ok = False
            try:
                async with (client.aio.live.connect(model=self._model, config=config) as session,
                            asyncio.TaskGroup() as tg):
                    self._session = session
                    connected_ok = True
                    consecutive_failures = 0
                    if self._offline:
                        self._offline = False
                        _log("[LIVE] network is back")
                    dt = time.time() - t0
                    self._t_connected = time.time()
                    _log("[LIVE] connected in %.1fs", dt)
                    if self._trace:
                        self._trace.mark("connected", seconds=round(dt, 2))

                    # Queues belong to this loop and must exist before the
                    # microphone callback can post to them.
                    self._mic_queue = asyncio.Queue(maxsize=MIC_QUEUE_FRAMES)
                    self._input_text_queue = asyncio.Queue()
                    # One frame deep on purpose: if a frame is still waiting
                    # when the next is captured, the waiting one is already
                    # out of date.
                    self._video_queue = asyncio.Queue(maxsize=1)
                    self._gate.reset()

                    # Consumers before producers.
                    #
                    # Previously the mic was opened and the greeting awaited
                    # before these tasks existed, so nothing drained the mic
                    # queue; everything the user said in the meantime was
                    # dropped, and what survived was transmitted stale.
                    tg.create_task(self._mic_sender(session))
                    tg.create_task(self._receiver(session))
                    tg.create_task(self._text_sender(session))
                    tg.create_task(self._video_sender(session))

                    # Devices next, and only then do we call ourselves ready.
                    #
                    # This ordering is the whole fix for NOVA speaking over her
                    # own startup. The greeting used to be sent as soon as the
                    # socket opened, while the speaker was still being opened
                    # on another thread and the interface had not finished
                    # loading; the first words went nowhere and what the user
                    # heard was a fragment. Nothing may be said until the
                    # session, the speaker and the microphone are all genuinely
                    # up.
                    speaker_ok = self._start_playback()
                    self._start_mic()
                    self._t_mic_ready = time.time()
                    if self._trace:
                        self._trace.mark("mic_ready")
                    with self._state_lock:
                        self._state = LiveState.STREAMING
                    self._publish(LiveEvent(
                        "state", state="connected", connect_time_s=round(dt, 2)))
                    _log("[LIVE] voice ready in %.2fs (connect %.2fs, speaker %s,"
                         " mic %s)", self._t_mic_ready - self._start_time, dt,
                         "ok" if speaker_ok else "unavailable",
                         "ok" if self._mic_active else "unavailable")
                    self._publish(LiveEvent("state", state="ready",
                                            speaker=speaker_ok,
                                            mic=self._mic_active))

                    # Greet once per session, not on every reconnect. The Live
                    # socket drops on keepalive timeout or the ten-minute
                    # session cap, so greeting on reconnect made NOVA introduce
                    # herself over and over to someone mid-conversation.
                    #
                    # Fired as a task, never awaited: composing the greeting can
                    # touch NOVA Core, and anything slow in the greeting path
                    # must not hold up the conversation the user is already
                    # trying to have.
                    if not self._has_greeted:
                        self._has_greeted = True
                        tg.create_task(self._send_greeting(session, speaker_ok))
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not connected_ok:
                    consecutive_failures += 1
                _log(
                    "[LIVE] connect/run error (failure %d/%d): %s",
                    consecutive_failures, self.MAX_RECONNECT_ATTEMPTS, e,
                )
                self._last_error = str(e)
                if self._is_auth_failure(str(e)):
                    # Permanent: report it in the user's language and stop.
                    self._auth_rejected = True
                elif self._is_offline(str(e)):
                    # Not a failure to count. Wait it out and say so plainly,
                    # rather than exhausting the retry budget while the user
                    # is on a train.
                    consecutive_failures = 0
                    if not self._offline:
                        self._offline = True
                        _log("[LIVE] no network; waiting for it to come back")
                        self._publish(LiveEvent(
                            "state", state="offline", error=str(e),
                            message=("NOVA can't reach the network, so voice "
                                     "is paused. She'll pick it up again as "
                                     "soon as you're back online.")))
                    self._publish(LiveEvent("state", state="offline"))
                else:
                    self._publish(LiveEvent("state", state="error", error=str(e)))
            finally:
                self._screen.stop()
                self._stop_mic()
                self._stop_playback()
                self._session = None

            # Reconnect on transient drops (keepalive timeout, network blip, 5xx)
            if not self._should_reconnect():
                break

            if self._auth_rejected:
                _log("[LIVE] credential rejected by Gemini; not retrying")
                with self._state_lock:
                    self._state = LiveState.ERROR
                self._publish(LiveEvent(
                    "state", state="error", gave_up=True, fatal="auth",
                    error=self._last_error,
                    message=("Gemini rejected NOVA's API key, so voice cannot "
                             "start. Open Settings and enter a valid key."),
                ))
                break

            if consecutive_failures >= self.MAX_RECONNECT_ATTEMPTS:
                _log(
                    "[LIVE] giving up after %d consecutive failures: %s",
                    consecutive_failures, self._last_error,
                )
                with self._state_lock:
                    self._state = LiveState.ERROR
                self._publish(LiveEvent(
                    "state", state="error", error=self._last_error, gave_up=True,
                ))
                break

            # Exponential backoff with a ceiling, so a persistent outage costs
            # a trickle of retries rather than 30 per minute. While offline
            # the backoff sits at a steady poll instead: there is no server to
            # be gentle with, and the user wants voice back the moment their
            # connection is.
            delay = (self.OFFLINE_RETRY_DELAY if self._offline else min(
                self.RECONNECT_BASE_DELAY * (2 ** max(0, consecutive_failures - 1)),
                self.RECONNECT_MAX_DELAY,
            ))
            with self._state_lock:
                self._state = LiveState.CONNECTING
            self._publish(LiveEvent("state", state="connecting", retry_in_s=round(delay, 1)))
            await asyncio.sleep(delay)

    async def _send_greeting(self, session: Any, speaker_ok: bool = True) -> None:
        """Say something worth saying — or just say hello.

        The greeting is NOVA Core's, not the session's. Core is asked whether it
        has anything genuinely worth surfacing (missed notices, system events);
        if it does, that becomes the opening line, otherwise NOVA gives a short
        natural greeting. Either way she then goes straight back to listening.

        Nothing is said until there is somewhere for it to go. That means a
        working speaker, and a surface actually attached to the event stream —
        the user reported NOVA talking before the window had even appeared, and
        the fragment they heard was the first half of a sentence played into a
        pipeline that was not finished being built.
        """
        if not speaker_ok:
            _log("[LIVE] no speaker; skipping the greeting")
            return
        if not await self._await_surface():
            _log("[LIVE] no interface attached after %.0fs; greeting anyway",
                 SURFACE_WAIT_S)
        await self._await_mic_stream()

        surfaced = ""
        try:
            # In a worker thread, with a deadline.
            #
            # opening_line() lazily imports nova, which imports google.genai:
            # measured at 7.4 seconds on a cold start. Run inline on the event
            # loop that froze the whole session -- the receiver, the mic sender
            # and the text sender could not run, so the user's first words were
            # buffered until the queue overflowed and then sent stale.
            surfaced = await asyncio.wait_for(
                asyncio.to_thread(_compose_opening_line), timeout=GREETING_BUDGET_S)
        except asyncio.TimeoutError:
            _log("[LIVE] opening line took too long; greeting plainly instead")
        except Exception as e:
            _log("[LIVE] opening line unavailable: %s", e)

        if surfaced:
            prompt = (
                "You have just come online and you have something worth telling the "
                f"user right now. Say it out loud, naturally, in one or two short "
                f"sentences, then ask what they need. Here is what to convey:\n\n{surfaced}"
            )
        else:
            meta = _load_meta()
            name = (meta.get("user_name") or "").strip() or "there"
            prompt = (
                f"The user just opened this session. Greet {name} right now, out loud, "
                "in 1-2 short spoken sentences — warm, a little informal, varying your "
                "phrasing rather than reusing a stock line. End by asking what they need. "
                "Do not wait for them to speak first."
            )

        try:
            if self._trace:
                self._trace.mark("greeting_sent")
            await session.send_client_content(
                turns={"role": "user", "parts": [{"text": prompt}]}, turn_complete=True
            )
            _log("[LIVE] opening sent (%s)", "proactive" if surfaced else "greeting")
        except Exception as e:
            _log("[LIVE] greeting failed: %s", e)

    async def _await_mic_stream(self) -> bool:
        """Wait until real microphone audio has reached the model.

        This ordering is load-bearing, not politeness. If client content is
        sent before the realtime audio stream is established, Gemini Live
        never activates audio input for that session: the model answers the
        text, and then every word the user says afterwards is ignored, for as
        long as the connection lasts. Measured directly — greeting first, the
        utterance that follows is not even transcribed; two seconds of
        microphone first, the same utterance is heard and answered.

        That is the whole of the "NOVA greeted me and then went deaf"
        report, and it is why the greeting waits here.
        """
        deadline = time.time() + MIC_WARMUP_WAIT_S
        while time.time() < deadline:
            if self._audio_bytes_in >= MIC_WARMUP_BYTES:
                return True
            await asyncio.sleep(0.05)
        _log("[LIVE] mic stream still quiet after %.1fs (%d bytes sent); "
             "greeting anyway", MIC_WARMUP_WAIT_S, self._audio_bytes_in)
        return False

    async def _await_surface(self) -> bool:
        """Wait until an interface is listening, or give up and speak anyway.

        Giving up matters as much as waiting. A headless run, or a window that
        failed to load, must not leave NOVA permanently mute — but nor should
        she deliver the greeting to nobody a full second before the window
        exists.
        """
        deadline = time.time() + SURFACE_WAIT_S
        while time.time() < deadline:
            with self._subs_lock:
                if self._subscribers:
                    return True
            await asyncio.sleep(0.1)
        return False

    async def _mic_sender(self, session: Any) -> None:
        while True:
            try:
                data = await self._mic_queue.get()
                if data is None:
                    break
                t0 = time.monotonic()
                await session.send_realtime_input(
                    audio=gtypes.Blob(data=data, mime_type="audio/pcm;rate=16000")
                )
                # A frame is 64 ms of audio. If handing it to the socket takes
                # longer than that we are falling behind in real time, and the
                # queue behind us starts discarding the user's words. Silence
                # here is how that went unnoticed for so long.
                dt = time.monotonic() - t0
                self._send_total += dt
                self._send_count += 1
                if dt > self._send_worst:
                    self._send_worst = dt
                if dt > 0.25:
                    _log("[LIVE] slow mic send: %.0f ms", dt * 1000)
                if not self._audio_bytes_in and self._trace:
                    self._trace.mark("first_user_audio")
                self._audio_bytes_in += len(data)
            except asyncio.CancelledError:
                break
            except Exception as e:
                _log("[LIVE] mic send error: %s", e)
                raise

    async def _receiver(self, session: Any) -> None:
        """Read the model's turns, forever — not just the first one.

        `session.receive()` is one *turn*, not the session. Look at the SDK:

            while result := await self._receive():
              if result.server_content and result.server_content.turn_complete:
                yield result
                break

        It breaks on turn_complete, so a bare `async for msg in
        session.receive()` reads exactly one reply and then falls off the end.
        That was NOVA's second failure, and it was every symptom at once:
        after her first answer she never read another message, so she never
        spoke again, never showed another transcript, and never noticed
        anything the user said. Worse, an unread socket is a stalled socket —
        the incoming queue fills, the library stops reading the transport,
        pongs stop being processed, and about seventy seconds later the
        connection dies with "keepalive ping timeout". Every one of those
        1011s in the logs traces back to this loop ending.

        The outer loop is what the reference implementation has and what this
        was missing.
        """
        # Transcript of the turn in progress, accumulated so a finished
        # exchange can be offered to memory as a whole rather than word by
        # word.
        heard: list[str] = []
        said: list[str] = []
        try:
            while True:
                got_turn = False
                async for msg in session.receive():
                    got_turn = True

                    if msg.tool_call and msg.tool_call.function_calls:
                        # Answer it, or the model simply stops.
                        #
                        # This session advertises sixteen tools and had no
                        # handler for any of them. Gemini Live blocks on a
                        # function call until the client returns a response,
                        # so the moment NOVA decided to look something up she
                        # went silent — no audio, no transcript, turn over.
                        # Conversational replies still worked, which is why it
                        # looked intermittent: "hello" was answered and "what
                        # is the capital of Nigeria" was not.
                        await self._handle_tool_calls(session, msg.tool_call)
                        continue

                    sc = msg.server_content
                    if sc is None:
                        continue

                    if sc.model_turn and sc.model_turn.parts:
                        for part in sc.model_turn.parts:
                            inline = getattr(part, "inline_data", None)
                            if inline is not None and getattr(inline, "data", None):
                                audio = inline.data
                                self._audio_bytes_out += len(audio)
                                if self._t_first_audio == 0.0:
                                    self._t_first_audio = time.time()
                                    if self._trace:
                                        self._trace.mark("first_model_audio")
                                if self._trace:
                                    self._trace.turn_timing("model audio")
                                # NOVA Core owns playback. Previously the raw PCM
                                # was shipped to the browser, which meant any
                                # surface with the page open played it — with the
                                # fullscreen window and the ambient orb both open
                                # that is the same audio twice.
                                self._enqueue_audio(audio)

                    if sc.input_transcription and sc.input_transcription.text:
                        text = sc.input_transcription.text.strip()
                        if text:
                            if self._trace:
                                self._trace.turn_started()
                            heard.append(text)
                            self._publish(LiveEvent("user_transcript", text=text))

                    if sc.output_transcription and sc.output_transcription.text:
                        text = sc.output_transcription.text.strip()
                        if text:
                            said.append(text)
                            self._publish(LiveEvent("nova_transcript", text=text))

                    if sc.interrupted:
                        # The model has acknowledged the cut and stopped
                        # generating. Anything of the old turn still queued is now
                        # stale, so drop it rather than playing a fragment of an
                        # answer the user has already moved on from.
                        self._play_generation += 1
                        nova_voice.drain(self._play_q)
                        self._publish(LiveEvent("interrupted"))
                        self._turn_count += 1

                    if sc.turn_complete:
                        self._turn_done_flag = True
                        self._turn_count += 1
                        self._publish(LiveEvent("turn_complete", turn_count=self._turn_count))
                        # Offer the finished exchange to long-term memory, off
                        # the event loop — deciding what is worth keeping can
                        # involve a model call and must never delay audio.
                        turn_user, turn_nova = " ".join(heard), " ".join(said)
                        heard, said = [], []
                        if turn_user:
                            threading.Thread(
                                target=_remember_turn, args=(turn_user, turn_nova),
                                name="nova-memory", daemon=True).start()

                    if msg.go_away:
                        self._publish(LiveEvent("go_away"))
                        raise ConnectionError("gemini live go_away")

                if not got_turn:
                    # receive() ended without yielding anything: the socket
                    # is gone. Spinning here would burn a core for nothing.
                    raise ConnectionError("gemini live stream ended")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            _log("[LIVE] receiver error: %s", e)
            self._publish(LiveEvent("error", error=str(e)))
            raise

    async def _handle_tool_calls(self, session: Any, tool_call: Any) -> None:
        """Run the tools the model asked for and send the results back."""
        names = [fc.name for fc in tool_call.function_calls]
        _log("[LIVE] tool call: %s", ", ".join(names))
        self._publish(LiveEvent("tool_call", tools=names))

        responses = []
        for fc in tool_call.function_calls:
            responses.append(await self._run_tool(fc))

        try:
            await session.send_tool_response(function_responses=responses)
        except Exception as e:
            # The tool ran; the model never heard about it and will stall.
            # Say so rather than leaving a silent NOVA to explain.
            _log("[LIVE] send_tool_response failed: %s", e)
            self._publish(LiveEvent("error", error="tool_response_failed",
                                    message=str(e)))

    async def _run_tool(self, fc: Any) -> Any:
        """Execute one tool call off the event loop.

        Tools open applications, drive the browser and touch the filesystem —
        all of it blocking, and none of it allowed to stall the audio stream
        that is running on this loop.
        """
        name = fc.name
        args = dict(fc.args or {})
        t0 = time.time()
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self._execute_tool, name, args),
                timeout=TOOL_TIMEOUT_S)
        except asyncio.TimeoutError:
            result = (f"'{name}' is taking longer than {TOOL_TIMEOUT_S:.0f} "
                      "seconds; it may still be running.")
            _log("[LIVE] tool %s timed out", name)
        except Exception as e:
            result = f"Tool '{name}' encountered an error: {str(e)[:200]}"
            _log("[LIVE] tool %s failed: %s", name, e)
        _log("[LIVE] tool %s finished in %.1fs", name, time.time() - t0)
        self._publish(LiveEvent("tool_result", tool=name,
                                summary=str(result)[:200]))
        response = result if isinstance(result, dict) else {"output": str(result)}
        return gtypes.FunctionResponse(id=fc.id, name=name, response=response)

    @staticmethod
    def _execute_tool(name: str, args: dict) -> Any:
        """NOVA Core owns what tools are and what they do; this only calls it."""
        import nova as _nova
        meta = _load_meta()
        result = _nova._execute_tool_sync(name, args, meta)
        if name == "remember_fact" and args.get("fact"):
            try:
                _nova.add_memory_fact(args["fact"], meta)
            except Exception:
                pass
        return result

    async def _video_sender(self, session: Any) -> None:
        """Put the screen in front of the model, as conversation context.

        Not `send_realtime_input(video=...)`. That is the obvious call and it
        silently does nothing useful — measured directly: capture a frame of
        this desktop, send it that way, ask "what application is on my
        screen", and the model answers "I can't see your screen". The same
        frame sent as `inline_data` inside client content comes back with
        "a terminal application, likely Windows PowerShell, displaying
        command-line operations and errors related to a Python project",
        which is exactly what was on it.

        `turn_complete=False` matters as much. The frame is *context*, not a
        question — NOVA should not announce what she can see every two
        seconds. Left pending, it is folded into whatever the user says next,
        so "what am I looking at?" is answered from the screen as it was a
        moment ago.
        """
        last_sent_turn = -1
        last_sent_at = 0.0
        while True:
            try:
                item = await self._video_queue.get()
                if item is None:
                    break
                jpeg, mime = item

                # One frame per conversational turn is the right granularity:
                # enough that a question about the screen sees the screen,
                # few enough that the context does not fill up with pictures
                # of an unchanged desktop.
                now = time.monotonic()
                same_turn = self._turn_count == last_sent_turn
                if same_turn and (now - last_sent_at) < SCREEN_REFRESH_S:
                    continue

                await session.send_client_content(
                    turns={"role": "user",
                           "parts": [{"inline_data": {"mime_type": mime,
                                                      "data": jpeg}}]},
                    turn_complete=False)
                last_sent_turn = self._turn_count
                last_sent_at = now
                self._publish(LiveEvent("screen_frame", kb=round(len(jpeg) / 1024, 1)))
            except asyncio.CancelledError:
                break
            except Exception as e:
                _log("[LIVE] screen frame send failed: %s", e)
                # Vision is an enhancement; losing it must never take the
                # conversation down with it.
                await asyncio.sleep(1.0)

    async def _text_sender(self, session: Any) -> None:
        while True:
            try:
                text = await self._input_text_queue.get()
                if text is None:
                    break
                await session.send_client_content(
                    turns={"role": "user", "parts": [{"text": text}]}, turn_complete=True
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                _log("[LIVE] text send error: %s", e)
                break


# ── module singleton ──────────────────────────────────────────────────────────
_manager: LiveManager | None = None


def get_live_manager() -> LiveManager:
    global _manager
    if _manager is None:
        _manager = LiveManager()
    return _manager
