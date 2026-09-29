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
import collections
import os
import queue
import threading
import time
import uuid
from enum import Enum
from typing import Any

import numpy as np

import agent_activity
import nova_voice
from desk.screen_share import ScreenShare, capture_once
from task_manager import describe_step as _tool_action

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

#: What the model is told when the user cuts NOVA off with nothing to follow
#: it — a keystroke, a button, "stop". Dropping the audio only silences the
#: speaker; the model is still generating, and without this the next thing
#: NOVA says is the middle of the answer nobody wanted to hear the end of.
#: Phrased as a stage direction rather than a question so there is nothing
#: here for her to answer out loud.
INTERRUPT_NOTICE = ("[SYSTEM: The user pressed stop. Abandon the previous "
                    "answer completely. Do not finish it, do not summarise "
                    "it, do not acknowledge this. Say nothing at all and wait "
                    "silently for what they say next.]")

#: How long audio belonging to an interrupted turn may still arrive.
#:
#: Dropping the play queue and aborting the sound device silences NOVA
#: instantly, and for a while that looked like the whole job. It is not.
#: Measured against Gemini Live from this machine: after the cut the model
#: went on sending for 2.52 s — 25 more chunks — because the interruption has
#: to cross the network and the audio already generated is in flight behind
#: it. Every one of those chunks was played, so NOVA finished two and a half
#: seconds of a sentence the user had already stopped, while every surface
#: said "listening".
#:
#: So the tail is discarded too. This value is only the safety net: the window
#: normally closes the moment the server confirms the cut, which is sooner.
#: It exists because the previous attempt at this latched until confirmation
#: and nothing else — and when a confirmation went missing, NOVA was silent
#: for the rest of the session. A window that expires cannot do that.
BARGE_IN_SUPPRESS_S = 3.0

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

#: ...except for looking at something, which is a conversational act.
#:
#: Thirty seconds is a reasonable wait for a web search nobody is listening
#: to. It is not a reasonable wait for "what's on my screen", where the user
#: has asked a question out loud and is waiting in silence for an answer. The
#: in-session path returns in well under a second; this bounds the cases that
#: still have to go to Core — the webcam, saving a frame, face work — so one
#: slow REST call cannot hold the conversation open.
VISION_TIMEOUT_S = 12.0

#: Shortest gap between two streamed screen frames: one a second, the rate
#: the realtime video channel is built for.
SCREEN_STREAM_MIN_S = 1.0

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

#: Below this peak level, a chunk is treated as silence for spectrum
#: purposes. Without a floor, a near-silent chunk's tiny magnitudes still
#: get normalised up to a full-scale band pattern below, which reads to the
#: orb as loud, textured speech when nothing is actually being said.
_SPECTRUM_SILENCE_FLOOR = 0.02


def _spectrum_bands(samples: "np.ndarray", level: float) -> list:
    """8-band magnitude spectrum for one audio chunk, 0..1 per band.

    A real FFT, not a synthetic pulse: the orb's shader wraps these bands
    around the form (bandAt() in orb3d.js) so the displacement traces the
    actual shape of what NOVA is saying rather than a uniform swell. Band
    edges are log-spaced (np.geomspace) so the 8 bands bias toward voice-
    relevant low/mid frequencies instead of spending most of them on
    inaudible highs, the way a linear split over a 24 kHz spectrum would.
    """
    if samples.size == 0 or level < _SPECTRUM_SILENCE_FLOOR:
        return [0.0] * 8
    windowed = samples.astype(np.float32) * np.hanning(samples.size)
    mags = np.abs(np.fft.rfft(windowed))
    if mags.size < 9:
        return [0.0] * 8
    edges = np.geomspace(1, mags.size, 9).astype(np.int64)
    bands = []
    for i in range(8):
        lo, hi = edges[i], max(edges[i] + 1, edges[i + 1])
        bands.append(float(mags[lo:hi].mean()) if hi > lo else 0.0)
    peak = max(bands) or 1.0
    return [round(min(1.0, b / peak), 3) for b in bands]


class VoiceTrace:
    """Stage timings for one voice session.

    Deliberately not per-chunk: a line for every 20 ms of audio buries the
    events that matter. This records the boundaries -- when the mic became
    ready, when the user's speech reached the model, when audio came back,
    when it was audible -- and reports the gaps between them, which is what
    "why was that slow" actually needs.
    """

    __slots__ = ("session_id", "t0", "stages", "_lock", "_turn", "_turn_t0",
                 "last_turn_ms")

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.t0 = time.time()
        self.stages: dict[str, float] = {}
        self._lock = threading.Lock()
        self._turn = 0
        self._turn_t0 = 0.0
        self.last_turn_ms = 0

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
        ms = (time.time() - self._turn_t0) * 1000
        if label == "playback started":
            # Speech to first sound: the LATENCY the HUD shows.
            self.last_turn_ms = round(ms)
        _log("[VOICE %s] turn %d: %-18s +%6.0f ms since speech",
             self.session_id, self._turn, label, ms)

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


def _confirmation_waiting() -> bool:
    """Is a "should I proceed?" question waiting for the person right now?"""
    try:
        from desk.confirm import store
        return bool(store.pending(limit=1))
    except Exception:
        return False


def _archive_turn(holder: dict, user_text: str, nova_text: str) -> None:
    """Keep the conversation itself, the way typed chat already is. Runs in a thread.

    Voice turns were only ever offered to fact extraction, so a spoken session
    left nothing behind: no conversation in the window's history, and the
    session archive -- what NOVA reads to know what you talked about last time
    -- recorded every desktop session as "Empty session". One conversation per
    voice session, stored locally in this account's own database.
    """
    user_text = (user_text or "").strip()
    nova_text = (nova_text or "").strip()
    if not user_text and not nova_text:
        return
    try:
        from desk import store as desk_store
        with holder.setdefault("_lock", threading.Lock()):
            if not holder.get("cid"):
                title = "Voice — " + (" ".join(user_text.split()[:6]) or time.strftime("%H:%M"))
                holder["cid"] = desk_store.new_conversation(title=title[:80])
            cid = holder["cid"]
            if user_text:
                desk_store.add_message(cid, "user", user_text, {"voice": True})
            if nova_text:
                desk_store.add_message(cid, "assistant", nova_text, {"voice": True})
    except Exception as e:
        _log("[LIVE] could not save the voice turn to history: %s", e)
    try:
        import nova as _nova
        mem = getattr(_nova, "_nova_memory", None)
        if mem is not None:
            if user_text:
                mem.log_turn("user", user_text)
            if nova_text:
                mem.log_turn("assistant", nova_text)
    except Exception as e:
        _log("[LIVE] could not add the voice turn to the session archive: %s", e)


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


class UplinkFilter:
    """Only the user's voice goes to the model.

    Everything the mic heard used to be streamed while NOVA was quiet: the
    room itself (as periodic keep-alives) and anything louder than it at full
    rate. Gemini's own detector then took a key press or a chair for speech
    and answered it -- "she picks up on the slightest sound". Now a frame
    that is not voice is sent as true silence, which also lets the model
    hear the end of the user's turn cleanly.

    The speech model decides a frame *after* hearing it, so the first
    syllable would be lost. The last few raw frames are therefore held back
    and sent ahead of the first voiced one.
    """

    def __init__(self, preroll_frames: int = 5) -> None:
        self._ring: collections.deque = collections.deque(maxlen=preroll_frames)

    def push(self, raw: bytes, gated: bytes, send_real: bool) -> list:
        """Returns [(payload, is_silence), ...] to hand to the batcher.

        ``raw`` is what the microphone heard, ``gated`` what the gate would
        send (silence while NOVA speaks), ``send_real`` whether this frame is
        the user's voice.
        """
        if not send_real:
            self._ring.append(raw)
            return [(bytes(len(gated)), True)]
        out = [(r, False) for r in self._ring] + [(gated, False)]
        self._ring.clear()
        return out

    def reset(self) -> None:
        self._ring.clear()


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


class VoiceSupervisor:
    """Restarts a voice session that gave up, once the network is back.

    The session has a retry budget, and exhausting it is reasonable -- six
    failures in two minutes usually means something is genuinely wrong. What
    is not reasonable is that exhausting it was *permanent*: on 2026-09-21
    NOVA gave up at 01:24:19 and the network returned at 01:25:13, and she
    then logged "api_healthy=True" every twenty seconds for six minutes with
    the microphone dead, because the session owns the microphone and nothing
    was watching for the chance to start again.

    This is the safety net rather than the fix. Classifying a handshake
    timeout as an outage keeps the budget from being spent in the first
    place; this catches whatever the classification misses, because a healthy
    network and a dead session should never sit side by side.

    Restarts are backed off, so a session that keeps dying costs a few
    attempts rather than a hot loop -- but the backoff resets with time, so a
    recovery an hour later is still tried.
    """

    #: Attempts before waiting for the cool-off.
    MAX_CONSECUTIVE = 3
    #: How long to wait after that, before trying again.
    COOLOFF_S = 600.0

    def __init__(self, live_factory=None, is_healthy=None) -> None:
        self._live_factory = live_factory or (lambda: get_live_manager())
        self._is_healthy = is_healthy or (lambda: True)
        self._attempts = 0
        self._last_attempt = 0.0
        self._now = time.time

    def check(self) -> bool:
        """Restart if it is dead and the network is up. Never raises."""
        try:
            if not self._is_healthy():
                return False

            manager = self._live_factory()
            if manager is None:
                return False

            status = manager.status() or {}
            if status.get("state") != "error" and not status.get("gave_up"):
                self._attempts = 0
                return False

            now = self._now()
            if self._attempts >= self.MAX_CONSECUTIVE:
                if (now - self._last_attempt) < self.COOLOFF_S:
                    return False
                self._attempts = 0      # the cool-off has passed; try again

            self._attempts += 1
            self._last_attempt = now
            _log("[LIVE] the network is back and the session had given up; "
                 "starting it again (attempt %d)", self._attempts)
            manager.start()
            return True
        except Exception:
            _log("[LIVE] could not check whether voice needs restarting")
            return False


#: Tools that are work rather than an action: they take seconds, and nothing
#: the user asked depends on NOVA going quiet until they finish. Declared
#: NON_BLOCKING to the live model so she keeps talking while they run; the
#: result is delivered WHEN_IDLE, so it never cuts her off mid-sentence.
#: Measured against gemini-3.1-flash-live-preview (2026-09-24): she spoke,
#: called the tool, and spoke the result once it arrived.
#:
#: Everything else stays blocking on purpose -- a quick action, or one that
#: needs the user's agreement, has an outcome she must not guess at.
NON_BLOCKING_TOOLS = frozenset({
    "web_search", "learn_resource", "file_processor", "generate_document",
    "browser_control", "nova_capability",
})


def _interrupt_preference() -> bool:
    """The user's "interrupt NOVA's speech" preference (on by default)."""
    try:
        from desk import settings as desk_settings
        return bool(desk_settings.get("barge_in", True))
    except Exception:
        return True


def _voice_barge_in_enabled() -> bool:
    """Can the user talk over NOVA to interrupt her?

    Yes by default: the echo-cancelling gate is what lets "stop" reach the
    model while she is speaking. It used to be off everywhere because it ran
    inside the PortAudio callback, where a loaded CPU dropped microphone
    blocks; it now runs on its own thread (see _start_mic), and costs 0.03 ms
    per 64 ms frame. NOVA_VOICE_FULL_DUPLEX overrides the setting either way.
    """
    env = os.getenv("NOVA_VOICE_FULL_DUPLEX", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    return _interrupt_preference()


def _live_tool_declarations(decls: list) -> list:
    """The live session's copy of NOVA's tool list, with slow work async.

    A copy: the same declarations serve the text chat, where there is no
    such thing as talking while a tool runs.
    """
    out = []
    for d in decls or []:
        d = dict(d)
        if d.get("name") in NON_BLOCKING_TOOLS:
            d["behavior"] = "NON_BLOCKING"
        out.append(d)
    return out


class ReconnectRequested(Exception):
    """Raised inside a session's TaskGroup to replace its connection.

    Raising is the only thing that ends a TaskGroup early: a task that simply
    returns cancels none of its siblings. The stall watchdog used to "drop the
    connection" by ending the mic sender, which left the receiver waiting on
    the dead socket for minutes while the user's speech piled up unsent.
    """


def _duration_s(value: Any) -> float | None:
    """Seconds in a protobuf Duration, as the SDK may give it: timedelta, number or "50s"."""
    if value is None:
        return None
    try:
        if hasattr(value, "total_seconds"):
            return float(value.total_seconds())
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip().lower()
        return float(text[:-1]) if text.endswith("s") else float(text)
    except (TypeError, ValueError):
        return None


def _is_requested_reconnect(e: BaseException) -> bool:
    if isinstance(e, ReconnectRequested):
        return True
    if isinstance(e, BaseExceptionGroup):
        return all(_is_requested_reconnect(x) for x in e.exceptions)
    return False


class ResponseWatchdog:
    """Notices that speech went up and nothing came back.

    From a real session: four turns over ninety seconds, every one of them
    heard and sent, none answered, and no complaint from anywhere. At teardown
    the receiver was still pending on a Future that never resolved and
    keepalive was still running, so the socket looked healthy from every angle
    the code checked. The existing reconnect fires on an exception, and a
    silent stall raises nothing.

    Deliberately dumb, because the clever version would be wrong: it does not
    try to tell an overloaded provider from a dead socket from a lost network.
    Speech went up, nothing came back, and after a while that is worth saying
    out loud whatever the cause.

    The clock is *not* reset by further speech. Restarting it each turn would
    mean a user who keeps trying never triggers it -- the harder they try to
    get an answer, the longer NOVA stays broken.
    """

    #: Turn 8 of that session took 15.4 s from speech to playback, and it was
    #: a good answer. Cutting a legitimately slow reply short would be worse
    #: than the bug this exists for.
    DEFAULT_TIMEOUT_S = 25.0

    def __init__(self, timeout_seconds: float = DEFAULT_TIMEOUT_S) -> None:
        self.timeout_seconds = float(timeout_seconds)
        self._awaiting_since = 0.0
        self._reported = False
        self._tools_running = 0
        self._now = time.time          # seam: tests drive this

    def user_spoke(self) -> None:
        """A turn began. Starts the clock, never restarts it."""
        if not self._awaiting_since:
            self._awaiting_since = self._now()

    def model_responded(self) -> None:
        self._awaiting_since = 0.0
        self._reported = False

    def reset(self) -> None:
        self._awaiting_since = 0.0
        self._reported = False
        self._tools_running = 0

    def tool_started(self) -> None:
        """The model answered with a tool call. Waiting on the tool is work,
        not silence: a 60 s research call must not read as a dead socket."""
        self._tools_running += 1
        self._awaiting_since = 0.0
        self._reported = False

    def tool_finished(self) -> None:
        """The result went back; now the model owes a reply to it."""
        self._tools_running = max(0, self._tools_running - 1)
        if not self._tools_running:
            self._awaiting_since = self._now()
            self._reported = False

    def waiting_for(self) -> float:
        if not self._awaiting_since or self._tools_running:
            return 0.0
        return max(0.0, self._now() - self._awaiting_since)

    def stalled(self) -> bool:
        return self.waiting_for() > self.timeout_seconds

    def take_stall(self) -> bool:
        """True once per stall, so the caller does not reconnect in a loop."""
        if self._reported or not self.stalled():
            return False
        self._reported = True
        return True

    def message(self) -> str:
        seconds = int(self.waiting_for())
        return (f"I heard you, but nothing came back for {seconds} seconds — "
                f"the connection looks stuck, so I'm reconnecting.")


class MicLevelThrottle:
    """Decides which microphone amplitudes are worth sending to the surface.

    The level is read inside the real-time audio callback, roughly fifty
    times a second. Publishing all of it would spend the socket on frames no
    one can see the difference between, and the callback has a deadline.

    Two exceptions to the fixed rate, both about honesty rather than volume:

    * The *onset* of speech goes immediately. Waiting up to 80 ms for the next
      tick is visible -- the orb reacts after the user has started talking,
      which reads as lag in the assistant rather than in the animation.
    * Silence is sent until it has settled and then stops. A quiet room does
      not need restating twenty times a second; but the first few frames of
      quiet do, or the orb stays lit after the user stops.
    """

    #: A jump this large counts as an onset and bypasses the rate limit.
    ONSET_DELTA = 0.12
    #: How many consecutive near-zero readings to send before going silent.
    SETTLE_SENDS = 2
    #: Below this, a reading counts as "nothing happening".
    QUIET_LEVEL = 0.01

    def __init__(self, hz: float = 12.0) -> None:
        self._interval = 1.0 / max(1.0, hz)
        self._last_sent_at = 0.0
        self._last_level = 0.0
        self._quiet_sends = 0
        self._now = time.time          # seam: tests drive this

    def should_send(self, level: float) -> bool:
        now = self._now()
        previous = self._last_level
        self._last_level = level

        if level < self.QUIET_LEVEL:
            if self._quiet_sends >= self.SETTLE_SENDS:
                return False
            self._quiet_sends += 1
            self._last_sent_at = now
            return True

        self._quiet_sends = 0

        if level - previous >= self.ONSET_DELTA:
            self._last_sent_at = now
            return True

        if (now - self._last_sent_at) >= self._interval:
            self._last_sent_at = now
            return True

        return False


def _log(msg: str, *a: Any) -> None:
    try:
        import nova as _nova
        _nova.log.info(msg, *a)
    except Exception:
        print(msg % a if a else msg)


#: Values that mean "nobody has said who this is". "User" is the shipped
#: default in desk/settings.py, so it arrives looking exactly like a real
#: answer — and greeting someone as "User" is worse than admitting you don't
#: know them.
_PLACEHOLDER_NAMES = frozenset({"", "user", "there", "none", "unknown", "unset"})


def _voice_language() -> str:
    """BCP-47 language the microphone is expected to be speaking."""
    try:
        from desk import settings as _s
        val = str(_s.get("voice_language", "") or "").strip()
        if val:
            return val
    except Exception:
        pass
    return os.getenv("NOVA_VOICE_LANGUAGE", "").strip() or "en-US"


def _preferred_host_api() -> int | None:
    """Index of the host API NOVA should open audio through, or None.

    PortAudio lists the same speaker several times, once per Windows audio
    API, and `sounddevice` defaults to whichever PortAudio calls first --
    which on Windows is MME, the oldest and worst of them. Measured on this
    machine, the same Realtek speaker:

        MME              90 ms minimum latency
        Windows WASAPI    3 ms

    MME is a compatibility shim over the modern stack. It buffers coarsely,
    which is heard as crackling, and it does not report a device disappearing
    in any useful way -- when headphones were unplugged mid-sentence it
    returned "there is no driver installed on your system", a message about
    nothing that is actually true.

    WASAPI is what Windows itself uses. Preferred where present; everything
    falls back to PortAudio's own default otherwise, because a worse audio
    API is still enormously better than no audio.
    """
    try:
        import sounddevice as _sd
        for i, api in enumerate(_sd.query_hostapis()):
            if "wasapi" in str(api.get("name", "")).lower():
                return i
    except Exception:
        pass
    return None


def _same_device_on(host_api: int, device_index: int | None, output: bool):
    """The given device as exposed by `host_api`, matched by name.

    Returns None when there is no equivalent, so the caller keeps whatever it
    already had rather than opening something that is not the user's speaker.
    """
    if host_api is None:
        return None
    key = "max_output_channels" if output else "max_input_channels"
    try:
        import sounddevice as _sd
        devices = _sd.query_devices()
        if device_index is None:
            device_index = _sd.default.device[1 if output else 0]
        if device_index is None or device_index < 0:
            return None
        want = str(devices[device_index].get("name", "")).strip().lower()
        if not want:
            return None
        for i, d in enumerate(devices):
            if (d.get("hostapi") == host_api and d.get(key, 0) > 0
                    and str(d.get("name", "")).strip().lower().startswith(want[:28])):
                return i
    except Exception:
        pass
    return None


def _wasapi_settings(device):
    """WASAPI options for `device`, or None if it is not a WASAPI device.

    `auto_convert` is the whole reason this exists. WASAPI shared mode only
    accepts the device's own mix format, and this machine's speaker runs at
    48 kHz while Gemini Live returns 24 kHz — so opening it plainly fails with
    "Invalid sample rate", which is exactly why audio had been coming out
    through MME instead. The flag turns on the resampler Windows already has.

    Measured on this machine, same speaker, writing Gemini-sized chunks:

        MME                       504 ms buffer, worst write 92 ms
        WASAPI + auto_convert     510 ms buffer, worst write 64 ms
    """
    try:
        import sounddevice as _sd
        api = _preferred_host_api()
        if api is None or device is None:
            return None
        if _sd.query_devices(device)["hostapi"] != api:
            return None
        return _sd.WasapiSettings(auto_convert=True)
    except Exception:
        return None


def _speaker_device():
    """Which output device to play through, resolved fresh every time.

    Fresh, because this is also the reconnect path: when a device disappears
    the answer to "which speaker now" has changed, and a cached index is the
    stale one that just died.
    """
    want = ""
    try:
        from desk import settings as _s
        want = str(_s.get("speaker_device", "") or "").strip()
    except Exception:
        pass
    want = want or os.getenv("NOVA_SPEAKER_DEVICE", "").strip()
    chosen = None
    if want:
        if want.isdigit():
            chosen = int(want)
        else:
            try:
                import sounddevice as _sd
                for i, d in enumerate(_sd.query_devices()):
                    if (d.get("max_output_channels", 0) > 0
                            and want.lower() in str(d.get("name", "")).lower()):
                        chosen = i
                        break
            except Exception as e:
                _log("[LIVE] could not resolve speaker_device %r: %s", want, e)
    upgraded = _same_device_on(_preferred_host_api(), chosen, output=True)
    return upgraded if upgraded is not None else chosen


def _device_label(device, output: bool) -> str:
    if device is None:
        return "system default"
    try:
        import sounddevice as _sd
        d = _sd.query_devices(device)
        api = _sd.query_hostapis(d["hostapi"])["name"]
        return f"{d['name']} via {api}"
    except Exception:
        return str(device)


def _mic_device():
    """Which input device to listen on, or None for the system default.

    Plugging in headphones can change which device Windows calls "default",
    and a machine can carry several inputs that all look plausible — a webcam
    array, a virtual cable, the built-in mic. Being able to name the one NOVA
    should use is the difference between "she cannot hear me" and a setting.

    Accepts an index, or any part of a device name (case-insensitive).
    Anything unrecognised falls back to the default rather than failing to
    open a microphone at all.
    """
    want = ""
    try:
        from desk import settings as _s
        want = str(_s.get("mic_device", "") or "").strip()
    except Exception:
        pass
    want = want or os.getenv("NOVA_MIC_DEVICE", "").strip()
    if not want:
        return None
    if want.isdigit():
        return int(want)
    try:
        import sounddevice as _sd
        for i, d in enumerate(_sd.query_devices()):
            if d.get("max_input_channels", 0) > 0 and want.lower() in d.get("name", "").lower():
                return i
    except Exception as e:
        _log("[LIVE] could not resolve mic_device %r: %s", want, e)
    _log("[LIVE] mic_device %r matched nothing; using the system default", want)
    return None


def _mic_device_resolved():
    """_mic_device, moved onto the better host API where one exists."""
    chosen = _mic_device()
    upgraded = _same_device_on(_preferred_host_api(), chosen, output=False)
    return upgraded if upgraded is not None else chosen


def _mic_device_name(device) -> str:
    try:
        import sounddevice as _sd
        info = _sd.query_devices(device if device is not None else None, "input")
        return str(info.get("name", "?"))
    except Exception:
        return "default input"


def _known_name(meta: dict) -> str:
    """The user's name, or empty when it was never actually given."""
    name = (meta.get("user_name") or "").strip()
    return "" if name.lower() in _PLACEHOLDER_NAMES else name


def _resolve(name: str, default: Any = None) -> Any:
    try:
        import nova as _nova
        return getattr(_nova, name, default)
    except Exception:
        return default


def _import_memory():
    """`memory_extra`, imported in an order that actually works.

    memory_extra imports nova, and nova imports memory_extra. Importing
    memory_extra *first* therefore fails with a partially-initialised module,
    and the failure was being swallowed into an empty dict — which is not
    "memory is unavailable", it is NOVA with amnesia. Every session that took
    this path began not knowing the user's name, having been told it many
    times, and asked again.

    The terminal never hit it because nova.py is its entry point, so nova was
    always fully imported first. The desktop is the other way round, which is
    a large part of why the two surfaces did not behave alike.

    Importing nova first breaks the cycle the same way the terminal does.
    """
    import nova            # noqa: F401  — resolves the cycle; do not remove
    import memory_extra
    return memory_extra


def _identity_block() -> str:
    """Who NOVA is talking to, stated rather than retrieved.

    This is deliberately not part of the semantic memory context below.
    Memory holds what NOVA has learned; this holds what she was told, and the
    difference matters: a name recalled by similarity search competes with
    four other remembered names and loses about as often as it wins. Asking
    someone their name for the fifth time is not a small flaw in an assistant
    meant to know them.
    """
    owner = None
    try:
        from nova_identity.people import PeopleRegistry
        owner = PeopleRegistry().owner()
    except Exception:
        owner = None

    if owner is not None and owner.names():
        # The registry knows the difference between a name and a form of
        # address, which the settings pair below cannot express. "Sir" is
        # what NOVA calls him; "Samuel Chibuzor Asagwara" is what belongs
        # in a box marked full name, and a model told only "the user is
        # Sir" will happily write that into one.
        name = owner.legal_name or owner.names()[0]
        lines = [f"You are talking to {name}."]
        other = [a for a in owner.aliases if a.lower() != name.lower()]
        if other:
            lines.append(f"{name} also goes by {', '.join(other)}. "
                         "They are all the same person.")
        if owner.preferred_address:
            lines.append(
                f"Address them as {owner.preferred_address!r} when "
                f"speaking to them. That is a form of address, not their "
                f"name: if anything asks for their name, it is {name}.")
        if owner.role:
            lines.append(f"{name} is {owner.role}.")
        lines.extend(_onboarding_profile_lines(name))
        lines.append(
            f"You already know who {name} is. Do not ask their name, and "
            "do not ask them to introduce themselves.")
        return "[WHO YOU ARE TALKING TO]\n" + "\n".join(lines)

    # No registry yet: the settings pair this replaced, unchanged, so an
    # installation where nobody has enrolled behaves exactly as before.
    try:
        from desk import settings as _s
        name = str(_s.get("user_name", "") or "").strip()
        role = str(_s.get("user_role", "") or "").strip()
    except Exception:
        return ""
    if not name or name.lower() in _PLACEHOLDER_NAMES:
        return ""
    lines = [f"You are talking to {name}."]
    if role:
        lines.append(f"{name} is {role}.")
    lines.extend(_onboarding_profile_lines(name))
    lines.append(
        f"You already know who {name} is. Do not ask their name, and do not "
        "ask them to introduce themselves.")
    return "[WHO YOU ARE TALKING TO]\n" + "\n".join(lines)


def _adaptation_block() -> str:
    """Layer B of nova_personality for the signed-in account."""
    try:
        import nova_personality
        from desk import settings as _s
        return nova_personality.adaptation_block(str(_s.get("response_style", "") or ""))
    except Exception:
        return ""


def _learn_preferences(text: str) -> None:
    """Explicit requests ("shorter answers", "no jokes") adjust Layer B."""
    try:
        import nova_personality
        changed = nova_personality.learn_from_message(text)
        if changed:
            _log("[LIVE] adapted to the user: %s", ", ".join(changed))
    except Exception:
        pass


_OCCUPATIONS = {"student": "a student", "developer": "a developer or engineer",
                "researcher": "a researcher", "business_owner": "a business owner",
                "professional": "a professional"}


def _onboarding_profile_lines(name: str) -> list:
    """What the person said about themselves when they set NOVA up. Stated,
    not retrieved, for the same reason as the name: it is what they told her."""
    try:
        from desk import settings as _s
        occ = str(_s.get("user_occupation", "") or "").strip().lower()
        about = str(_s.get("user_about", "") or "").strip()
    except Exception:
        return []
    out = []
    if occ in _OCCUPATIONS:
        out.append(f"{name} describes themselves as {_OCCUPATIONS[occ]}.")
    if about:
        out.append(f"In their own words, when they set you up: \"{about[:1500]}\"")
    return out


def _load_meta() -> dict:
    try:
        return _import_memory().load_memory()
    except Exception as e:
        # Loudly. Amnesia that announces itself is a bug report; amnesia that
        # does not is NOVA seeming not to know someone she has known for weeks.
        _log("[LIVE] could not load memory, so NOVA starts this session "
             "knowing nothing about the user: %s", e)
        return {}


def _build_memory_context(meta: dict) -> str:
    try:
        return _import_memory().build_memory_context(meta)
    except Exception as e:
        _log("[LIVE] memory context unavailable: %s", e)
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
            simple=not _voice_barge_in_enabled(),
            speech_detector=nova_voice.SpeechDetector(),
        )
        #: Decides what reaches the model while NOVA is quiet: the user's
        #: voice (with a little pre-roll), else true silence. See UplinkFilter.
        self._uplink = UplinkFilter()
        self._loud_reported = False
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
        #: Rate-limits microphone amplitude on its way to the orb.
        self._mic_level_throttle = MicLevelThrottle()
        #: Notices a session that has gone quiet without erroring.
        self._watchdog = ResponseWatchdog()
        #: Set to tear the current connection down and open a new one.
        self._drop_event: asyncio.Event | None = None
        self._drop_reason = ""
        #: Session resumption. Gemini ends every Live connection after a few
        #: minutes (GoAway, then a hard 1008 close). The newest resumable
        #: handle lets the next connection continue the same conversation
        #: instead of starting a stranger with no memory of it.
        self._resume_handle: str | None = None
        #: Set by GoAway: reconnect at the next quiet moment, before this.
        self._go_away_deadline: float | None = None
        #: Tool calls running beside the receiver, and ones Gemini withdrew.
        self._tool_tasks: set = set()
        self._cancelled_tool_ids: set = set()
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
        #: Consecutive failed writes that forced the device to be reopened.
        self._speaker_reopens = 0
        #: Times the speaker ran dry while a turn was still in progress. The
        #: audible half of this is crackling, and a zero here means the gap
        #: was not introduced by NOVA.
        self._starved = 0
        # Bumped whenever playback is cut. The pre-roll lives inside the
        # speaker thread, so draining _play_q alone would leave held audio to
        # play after an interruption -- exactly the "speaks a fragment of the
        # old response" behaviour barge-in exists to prevent.
        self._play_generation = 0
        #: Monotonic deadline until which incoming audio is the tail of a turn
        #: that has been interrupted, and is thrown away. Zero when not
        #: interrupting. Cleared early by the model confirming the cut.
        self._suppress_until = 0.0
        #: Chunks discarded that way, for the log.
        self._suppressed_tail = 0
        self._trace: VoiceTrace | None = None
        self._auth_rejected = False
        self._offline = False
        # Screen awareness shares this session rather than opening its own.
        # Ambient mode is a second view of one NOVA, so what she can see has
        # to arrive in the same conversation she is already having.
        self._screen = ScreenShare(sink=self._send_screen_frame, log=_log)
        self._video_queue: asyncio.Queue | None = None
        #: A frame captured because the user asked to be looked at, waiting
        #: for the turn that acknowledged the request to finish so it can be
        #: put in front of the model. (jpeg, mime, question).
        self._pending_vision: tuple[bytes, str, str] | None = None
        #: True from the capture until the model has been shown the frame.
        #: Without it a second 'vision' call arriving mid-capture stacks a
        #: second screenshot behind the first, and NOVA describes the screen
        #: twice.
        self._vision_busy = False
        #: When that started, so a flag left behind by a turn that never
        #: completed cannot lock the fast path out for the whole session.
        self._vision_busy_at = 0.0
        #: When NOVA last looked at anything, for the repeat-call cooldown.
        self._vision_last_at = 0.0

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
            self._voice_history = {}          # a new voice session is a new conversation
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
        # Stop the speaker here, explicitly.
        #
        # This used to be left to the `finally` inside _connect_and_run. But
        # stop() ends the session by stopping the event loop, and a coroutine
        # suspended when its loop stops is never resumed — so that `finally`
        # did not run, `_play_thread` was never cleared, and the *next*
        # session hit the guard at the top of _start_playback, returned False
        # without logging anything, and ran with no speaker at all.
        #
        # The effect was NOVA going permanently mute after the first restart:
        # "voice ready (speaker unavailable, mic ok)" and then "no speaker;
        # skipping the greeting", for every session until the app itself was
        # restarted. Seen five times in eight starts in one day's log.
        self._stop_playback()
        self._session = None
        # Leave nothing of this session behind for the next one to find.
        #
        # The loop and its queues outlived stop(), and a straggler from the
        # old session — a receive coroutine the SDK had not finished
        # unwinding — could still reach for them. Once that loop is closed,
        # touching it raises "Event loop is closed", which was seen published
        # as a fatal voice error moments before a perfectly healthy reconnect.
        # Cleared, the same straggler finds nothing and gives up quietly,
        # which is what a dead session's remains should do.
        self._loop = None
        self._mic_queue = None
        self._input_text_queue = None
        self._video_queue = None
        self._thread = None
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
            "last_turn_ms": self._trace.last_turn_ms if self._trace else 0,
            "uptime_s": uptime,
            "turns": self._turn_count,
            "audio_out_kb": round(self._audio_bytes_out / 1024, 1),
            "audio_in_kb": round(self._audio_bytes_in / 1024, 1),
            # What the microphone gate actually did with those bytes.
            #
            # audio_in_kb counts everything handed to the socket, and the gate
            # transmits *silence* rather than nothing while NOVA is speaking —
            # so a session can report megabytes sent while the model has heard
            # nothing but digital zeroes. That is indistinguishable from a
            # working microphone unless these are visible, and it is exactly
            # the shape of "she talks but cannot hear me".
            "mic": self._gate_stats(),
            # Zero means NOVA handed the device a continuous stream. Anything
            # else is her starving it, and is the half of "it crackles" that
            # is hers to fix.
            "speaker_starved": int(getattr(self, "_starved", 0)),
            "screen": self._screen.status(),
            "error": self._last_error,
        }

    def _gate_stats(self) -> dict:
        g = getattr(self, "_gate", None)
        if g is None:
            return {"available": False}
        sent = int(getattr(g, "frames_sent", 0))
        muted = int(getattr(g, "frames_muted", 0))
        total = sent + muted
        return {
            "available": True,
            "frames_sent": sent,
            "frames_muted": muted,
            "heard_pct": round(100.0 * sent / total, 1) if total else 0.0,
            "barge_ins": int(getattr(g, "barge_ins", 0)),
            "speaking": bool(getattr(g, "speaking", False)),
            "muted": bool(getattr(g, "muted", False)),
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

    def _open_output_stream(self):
        """Open the speaker on whatever device is current, and say which.

        Tries the better host API first and falls back to PortAudio's own
        default. The fallback is not decoration: a worse audio API is still
        enormously better than no audio, and this runs on the reconnect path
        where the device landscape has just changed underfoot.
        """
        attempts = []
        device = _speaker_device()
        if device is not None:
            attempts.append((device, _wasapi_settings(device)))
        attempts.append((None, None))           # PortAudio's default, as before

        last = None
        for dev, extra in attempts:
            try:
                stream = sd.RawOutputStream(
                    samplerate=nova_voice.RECEIVE_RATE,
                    channels=nova_voice.CHANNELS,
                    dtype="int16",
                    blocksize=0,
                    latency=OUTPUT_LATENCY_S,
                    device=dev,
                    extra_settings=extra,
                )
                stream.start()
                _log("[LIVE] speaker open on %s (%.0f ms buffer)",
                     _device_label(dev, True), (stream.latency or 0) * 1000)
                return stream
            except Exception as e:
                last = e
                _log("[LIVE] could not open speaker on %s: %s",
                     _device_label(dev, True), e)
        raise last if last else RuntimeError("no output device")

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
        if not HAS_SD:
            _log("[LIVE] no audio backend: NOVA cannot speak")
            return False
        if self._play_thread is not None:
            # Never fail silently here. Whatever left a worker behind, the
            # answer is to clear it and open the speaker — a session that
            # cannot talk is not a session, and it must not be reached by
            # returning False from a bare guard with nothing in the log.
            _log("[LIVE] speaker thread left over from a previous session; "
                 "stopping it before opening the device")
            self._stop_playback()
        self._play_stop.clear()
        self._speaker_reopens = 0
        self._starved = 0
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
            stream = self._open_output_stream()
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
            # `stream` is rebound when a lost device is reopened, and both
            # this function and emit() below have to see the new one. Without
            # the declaration the assignment makes it a local of _worker and
            # every earlier read of it — including the one inside emit —
            # becomes an unbound local.
            nonlocal stream

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
                        # Ran out of audio while the model was still sending.
                        #
                        # This is the software half of "it crackles", and it is
                        # worth separating from the other half. If the queue is
                        # empty before the turn is finished, the device is
                        # being starved by us — the network, or a stall on this
                        # thread — and the gap is audible. If this counter
                        # stays at zero and the user still hears breakup, the
                        # audio left here intact and the fault is downstream:
                        # the device, its driver, or a Bluetooth link.
                        # An empty queue is not yet a gap. The device still
                        # holds up to half a second of audio, and refilling it
                        # inside that window is inaudible — which is what the
                        # cushion is for. Only once the device has drained too
                        # is there actual silence in the room.
                        #
                        # Counting the queue alone reported starvation on
                        # every ordinary network wobble and would have sent us
                        # looking for a fault that nobody could hear.
                        if (primed and not self._turn_done_flag
                                and self._playback_drained()):
                            self._starved += 1
                            if self._starved in (1, 10, 50, 200):
                                _log("[LIVE] speaker ran dry mid-turn "
                                     "(%d time(s)) — audio arrived slower than "
                                     "it plays and the device emptied", self._starved)
                        if self._play_generation != generation:
                            generation = self._play_generation
                            preroll.clear(); preroll_bytes = 0; primed = False
                            continue
                        # A pause with audio still held back means the turn is
                        # short: play what there is rather than waiting for a
                        # cushion that will never fill.
                        if preroll and not primed:
                            drained_ok = True
                            for held in preroll:
                                try:
                                    emit(held)
                                except Exception as e:
                                    # Same device-loss handling as the main
                                    # write path: this branch plays out a
                                    # short turn, and a device that died
                                    # during it is no less dead.
                                    drained_ok = self._reopen_output(stream, e)
                                    break
                            preroll.clear(); preroll_bytes = 0
                            primed = drained_ok
                            if not drained_ok:
                                break
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
                        # The device went away. Reopen it; do not keep
                        # writing to a corpse.
                        #
                        # Unplugging headphones mid-sentence destroys the
                        # stream, and every write after that failed with
                        # "there is no driver installed on your system" --
                        # logged, ignored, and retried forever. NOVA was
                        # silent from that moment until the app was
                        # restarted, including for typed messages, because
                        # the audio for those went to the same dead device.
                        #
                        # Worse, `speaking` stays set while audio is still
                        # arriving, and `speaking` mutes the microphone. So a
                        # speaker that died also stopped her hearing anything
                        # -- which is exactly how it was reported: "I removed
                        # the headphones and then nothing at all."
                        if not self._reopen_output(stream, e):
                            break
                        stream = self._stream
                        preroll.clear(); preroll_bytes = 0; primed = False
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

    #: Consecutive device reopens before NOVA stops trying.
    #:
    #: A device that has genuinely gone (the only sound card removed) must not
    #: spin forever; a device that is merely changing (headphones out, Bluetooth
    #: connecting, Windows switching default) needs more than one attempt,
    #: because the new default is not always ready the instant the old one dies.
    MAX_SPEAKER_REOPENS = 5

    def _reopen_output(self, dead, err: Exception) -> bool:
        """Swap a dead output stream for a live one. False if we give up."""
        self._speaker_reopens += 1
        _log("[LIVE] speaker write failed (%s); reopening (attempt %d/%d)",
             err, self._speaker_reopens, self.MAX_SPEAKER_REOPENS)
        with self._stream_lock:
            try:
                dead.abort(); dead.close()
            except Exception:
                pass
            self._stream = None
        self._playing_until = 0.0
        # Whatever was mid-flight belonged to a device that no longer exists.
        # Let the mic back in immediately rather than after the watchdog:
        # `speaking` mutes it, and a broken speaker must never make NOVA deaf.
        self._gate.set_speaking(False, interrupted=True)
        nova_voice.drain(self._play_q)

        if self._speaker_reopens > self.MAX_SPEAKER_REOPENS:
            _log("[LIVE] giving up on the speaker after %d attempts",
                 self._speaker_reopens)
            self._speaker_alive = False
            self._publish(LiveEvent(
                "error", error="speaker_lost",
                message=("NOVA lost your audio output device and could not "
                         "reopen it. Check your speakers or headphones, then "
                         "restart voice.")))
            self._publish(LiveEvent("state", state="error",
                                    error="speaker_lost", fatal="audio"))
            return False

        # Windows needs a moment to settle on a new default after a device
        # is removed; asking immediately reliably returns the old one.
        time.sleep(0.4)
        try:
            self._stream = self._open_output_stream()
        except Exception as e2:
            _log("[LIVE] could not reopen the speaker: %s", e2)
            self._speaker_alive = False
            return False
        self._speaker_alive = True
        self._publish(LiveEvent("speaker_changed",
                                device=_device_label(_speaker_device(), True)))
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
        """Stop the speaker and wait until it has actually let go of the device.

        Returning before the worker has finished is how the next session found
        the sound device still open. The worker closes the stream in its own
        `finally`, so until it has run there is a live output stream on a
        device the next `RawOutputStream` is about to ask for.
        """
        self._play_stop.set()
        try:
            self._play_q.put_nowait(None)
        except Exception:
            pass
        t = self._play_thread
        self._play_thread = None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
            if t.is_alive():
                _log("[LIVE] speaker thread did not stop within 2s")
        self._speaker_alive = False

    def _enqueue_audio(self, audio: bytes) -> None:
        """Play a chunk and tell the surfaces how loud it is — unless the turn
        it belongs to has been interrupted, in which case throw it away.

        That discarding has a history worth knowing. It used to be an epoch
        latch that dropped every chunk of a turn once it had been cut,
        because the barge-in detector was firing on NOVA's own echo and
        playback had to be kept from resuming and re-triggering it. When the
        latch failed to clear, NOVA went silent for the rest of the session
        while the model went on replying, so it was removed.

        It is back because removing it was measured to be wrong: after a cut
        the model goes on sending for up to 2.5 s, and all of it was played.
        What is different is that the window closes. The model confirming the
        cut ends it — about a tenth of a second, measured — and a deadline
        ends it even if no confirmation ever comes, which is the failure the
        old latch could not survive.
        """
        if self._suppress_until:
            if time.monotonic() < self._suppress_until:
                # The turn was cut; this is the rest of it, still arriving.
                self._suppressed_tail += 1
                return
            # The safety net, not the usual exit. The model never confirmed
            # the cut, so stop assuming it is about to and let her be heard —
            # a stuck window is how NOVA went silent for a whole session.
            _log("[LIVE] interruption window expired unconfirmed after %d "
                 "discarded chunks", self._suppressed_tail)
            self._suppress_until = 0.0
        if not self._speaker_alive:
            # No playback thread: never mute the mic on its behalf, or NOVA
            # would be permanently deaf on a machine with no working speaker.
            self._publish(LiveEvent("error", error="speaker_unavailable"))
            return
        self._last_audio_at = time.time()
        # Announce the transition, not every chunk.
        #
        # Speaking is the longest-lived state in a conversation and the one
        # the user most needs to see, because it is what tells them whether
        # to wait or to talk. Every surface has handled it since it was
        # written; nothing ever sent it, so the orb said "listening" for the
        # whole time NOVA was audibly talking.
        #
        # Published here rather than in the playback worker because this is
        # where the decision is made: audio exists, the speaker is alive, so
        # NOVA is about to be heard. `speaking` is asked of the gate first
        # because the gate is the one authority on it — and because a state
        # event per chunk would be hundreds a turn, crossing the same
        # WebSocket as the audio it is describing.
        if not self._gate.speaking:
            self._publish(LiveEvent("state", state="speaking"))
        self._gate.set_speaking(True)
        try:
            self._play_q.put_nowait(audio)
        except queue.Full:
            pass
        # Amplitude drives the orb on every surface. Cheap: one pass over a
        # decimated view, not the whole buffer.
        #
        # The orb's shader (orb3d.js) has had full 8-band spectrum
        # reactivity since it was written -- uBands, bandAt() wrapping the
        # displacement around the form so a voice reads as shape, not just
        # a pulse -- and setSpectrum() to feed it. Nothing has ever called
        # setSpectrum(): only this scalar peak has ever been published, so
        # that shader code has been dead since it landed. A real FFT on the
        # same chunk already being measured for level costs microseconds
        # and gives the frontend something to actually deform against.
        try:
            samples = np.frombuffer(audio, dtype=np.int16)
            if samples.size:
                level = float(np.abs(samples[::16]).max()) / 32768.0
            else:
                level = 0.0
            bands = _spectrum_bands(samples, level)
        except Exception:
            level = 0.0
            bands = [0.0] * 8
        self._publish(LiveEvent("audio_level", level=round(level, 4), bands=bands))

    def _barge_in(self) -> None:
        """User spoke over NOVA — stop playing and let them finish."""
        # Clear speaking here too, not just in VoiceGate.process. "Stop
        # talking" has to be self-sufficient: any caller of _barge_in should
        # leave NOVA genuinely stopped, not rely on the detector having
        # already done half the work.
        # interrupted=True: the user is mid-sentence, so the speaker-tail
        # cooldown must not swallow the next quarter second of it.
        # Everything the model sends from here until it acknowledges the cut
        # belongs to the sentence being stopped — unless it has already said
        # the turn was over, in which case nothing more of it is coming and
        # the next audio is the next answer. Swallowing the opening of that is
        # the deafness this window exists to avoid, so ask before clearing the
        # flag below.
        if not self._turn_done_flag:
            self._suppress_until = time.monotonic() + BARGE_IN_SUPPRESS_S
            self._suppressed_tail = 0
        self._gate.set_speaking(False, interrupted=True)
        self._turn_done_flag = False
        self._play_generation += 1        # discard anything held in the pre-roll
        dropped = nova_voice.drain(self._play_q)
        discarded_ms = self._abort_playback()
        trig = self._gate.last_trigger or {}
        _log("[LIVE] barge-in — dropped %d queued chunks, %d ms already in "
             "the sound device; voice %.2f, level %s vs room %s, echo path %s",
             dropped, discarded_ms, trig.get("voice_prob", -1.0),
             trig.get("smoothed"), trig.get("noise_floor"), trig.get("echo_path"))
        self._publish(LiveEvent("playback_cancelled", queued_chunks=dropped,
                                device_ms=discarded_ms))
        # Every interruption says what it was measured on.
        #
        # A barge-in and a false barge-in are the same event from outside —
        # NOVA stops talking — and telling them apart afterwards is the
        # difference between tuning the detector and guessing at it. The
        # deciding number is ref_rms: playback loud at the moment of the cut
        # means the sound was most likely NOVA's own, and playback silent
        # means it was the room or the user.
        self._publish(LiveEvent("interrupted", reason="barge_in",
                                **self._gate.last_trigger))
        self._publish(LiveEvent("state", state="listening"))

    def _end_suppression(self, why: str) -> None:
        """Stop discarding the tail of an interrupted turn.

        Called when the model says the turn is over, by either route. Silent
        when no interruption is in progress, which is nearly always.
        """
        if not self._suppress_until:
            return
        self._suppress_until = 0.0
        if self._suppressed_tail:
            _log("[LIVE] interruption: discarded %d chunks of the cut turn "
                 "(%s)", self._suppressed_tail, why)
        self._suppressed_tail = 0

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

    @property
    def speaking(self) -> bool:
        """Is NOVA producing sound right now?

        Asked by surfaces deciding whether an interrupt would do anything.
        """
        return bool(self._gate.speaking)

    def barge_in(self, *, notify_model: bool = True) -> dict:
        """Stop talking, because the user asked — by key, button or voice.

        The same path the automatic detector uses, so a deliberate
        interruption and a detected one leave NOVA in identical states. There
        is no second way to stop her, which is the point: two ways to cancel
        speech is two sets of edge cases.

        Two halves, and both matter. Dropping the audio already produced is
        what makes the room go quiet; telling the model is what stops it
        generating the rest of a reply nobody is listening to any more.

        `notify_model=False` is for the caller that is about to send the
        user's own words: those interrupt the model by themselves, and a
        synthetic notice as well would have NOVA answer twice — an
        acknowledgement of the interruption, then the real answer.
        """
        if not self._gate.speaking:
            return {"ok": True, "message": "not speaking"}
        self._barge_in()
        if notify_model:
            self._post_to_loop(self._input_text_queue, INTERRUPT_NOTICE)
        return {"ok": True, "message": "interrupted"}

    @property
    def owns_microphone(self) -> bool:
        """Is this session holding the microphone open right now?

        Asked by the push-to-talk path before it opens one of its own. Two
        capture streams on one device is the "hidden audio managers fighting
        over the microphone" case: on Windows the second open usually
        succeeds, and what follows is two readers splitting the same input
        with neither getting a clean signal — a failure that presents as NOVA
        intermittently mishearing rather than as an error anyone can see.
        """
        return bool(self._mic_active)

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
        self._uplink.reset()

        def _handle(samples):
            # Every rule about what reaches the model lives in nova_voice:
            # mute-while-speaking, echo cancellation, barge-in detection.
            # Runs on the nova-mic-gate thread, never the audio callback.
            try:
                frame = self._gate.process(samples)
                # The gate has just measured this frame against the room floor
                # in order to decide what to transmit. Pass the same number to
                # the surface so the orb can show that someone is talking --
                # it was previously driven only by NOVA's *outgoing* audio, so
                # it sat still during the one moment it most needed to look
                # like it was listening. Throttled: this is a real-time
                # callback and the socket does not need fifty updates a
                # second.
                # Has NOVA gone quiet without erroring? This callback already
                # runs constantly, so noticing costs a comparison and needs no
                # extra thread. The work itself is handed to the session loop:
                # nothing slow or blocking belongs on the audio thread.
                if self._watchdog.take_stall():
                    self._on_stall()

                gate = self._gate
                # The orb shows the user's voice on a visible scale, and room
                # noise not at all; raw RMS put normal speech at ~0.14.
                level = (gate.last_voice_level if gate.hears_speech
                         else gate.last_level)
                # "Hearing" means a voice when there is a speech model to say
                # so -- the orb lighting up for a key press is the same
                # mistake as NOVA answering one.
                hearing = (gate.last_is_voice if gate.hears_speech
                           else not gate.last_was_quiet)
                if self._mic_level_throttle.should_send(level):
                    self._publish(LiveEvent(
                        "mic_level",
                        level=round(level, 4),
                        hearing=hearing,
                    ))
                if gate.hears_speech:
                    # Only a voice reaches the model; everything else is true
                    # silence. While NOVA speaks the gate has already muted
                    # the frame, and the raw audio is kept as pre-roll for a
                    # barge-in.
                    #
                    # Extremely loud sounds are *not* sent: measured on this
                    # laptop's mic, desk bangs and typing hit 10,000-26,000
                    # RMS nineteen times in thirteen seconds -- forwarding
                    # them is the "reacts to every sound" fault again. They
                    # are reported as an event instead.
                    send_real = not gate.last_was_silence and gate.last_is_voice
                    pieces = self._uplink.push(samples.tobytes(), frame, send_real)
                    if gate.last_is_loud_event and not self._loud_reported:
                        self._loud_reported = True
                        self._publish(LiveEvent("loud_sound",
                                                level=round(gate.last_level, 4)))
                    elif not gate.last_is_loud_event:
                        self._loud_reported = False
                else:
                    # Two kinds of nothing: the gate muting NOVA's own voice,
                    # and a room with no one talking in it. Both are zeroes as
                    # far as the model is concerned.
                    pieces = [(frame, gate.last_was_silence or gate.last_was_quiet)]
                payloads = [p for p in (batcher.add(b, s) for b, s in pieces)
                            if p is not None]
            except Exception:
                return
            if not payloads:
                return                      # still gathering, or throttled
            if loop is None or q is None or loop.is_closed():
                return

            def _post(payload):
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
                for payload in payloads:
                    loop.call_soon_threadsafe(_post, payload)
            except RuntimeError:
                pass            # loop shutting down

        # The PortAudio callback only hands the frame over. Echo cancellation,
        # barge-in, the stall check and batching all run on nova-mic-gate:
        # inside the callback they had a hard real-time budget, and on a busy
        # machine blowing it meant Windows dropped microphone blocks -- which
        # is why voice barge-in had been switched off altogether.
        raw: queue.Queue = queue.Queue(maxsize=MIC_QUEUE_FRAMES)
        self._mic_raw = raw

        def _cb(indata, frames, time_info, status):
            samples = np.array(indata, dtype=np.int16, copy=True).reshape(-1)
            try:
                raw.put_nowait(samples)
            except queue.Full:
                # Newest wins, as with the send queue: a live conversation.
                try:
                    raw.get_nowait()
                    raw.put_nowait(samples)
                    self._mic_dropped += 1
                except Exception:
                    pass

        def _gate_worker():
            while True:
                samples = raw.get()
                if samples is None:
                    return
                _handle(samples)

        self._mic_worker = threading.Thread(
            target=_gate_worker, name="nova-mic-gate", daemon=True)
        self._mic_worker.start()

        # None is a *device*, not a failure — it means "the system default",
        # and it is the fallback that matters most. An earlier version used it
        # as the "nothing opened" sentinel, so a successful fallback open was
        # read as a failure and the first attempt's error was raised over the
        # top of a working microphone.
        got = False
        last_err = None
        attempts = []
        _resolved = _mic_device_resolved()
        if _resolved is not None:
            attempts.append((_resolved, _wasapi_settings(_resolved)))
        attempts.append((_mic_device(), None))      # what it used to do
        opened = None
        try:
            for dev, extra in attempts:
                try:
                    self._mic_stream = sd.InputStream(
                        samplerate=MIC_RATE, channels=1, dtype="int16",
                        blocksize=MIC_BLOCK, callback=_cb, device=dev,
                        extra_settings=extra,
                    )
                    self._mic_stream.start()
                    opened, got = dev, True
                    break
                except Exception as e:
                    last_err = e
                    # Not a fault worth alarming about on its own: WASAPI
                    # input is refused outright by some drivers (this one
                    # answers with a WDM-KS ioctl error), and the next
                    # attempt is the one that has always worked.
                    _log("[LIVE] mic not available on %s: %s",
                         _device_label(dev, False), e)
            if not got:
                raise last_err if last_err else RuntimeError("no input device")
            self._mic_active = True
            self._mic_dropped = 0
            _log("[LIVE] mic opened (%s, 16 kHz mono, %d-frame blocks, "
                 "%.1fs queue, %s)", _device_label(opened, False), MIC_BLOCK,
                 MIC_QUEUE_FRAMES * MIC_BLOCK / MIC_RATE,
                 ("half-duplex: no voice barge-in" if self._gate.simple
                  else "voice barge-in on")
                 + (", speech detector on" if self._gate.hears_speech
                    else ", speech detector UNAVAILABLE: reacting to loudness"))
        except Exception as e:
            # Same reasoning as above: a device that is missing, busy or
            # blocked by Windows privacy settings must surface, not vanish
            # into a log line nobody reads.
            _log("[LIVE] mic open failed: %s", e)
            raw.put(None)               # nothing will feed the gate worker
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
        raw, worker = getattr(self, "_mic_raw", None), getattr(self, "_mic_worker", None)
        if raw is not None:
            nova_voice.drain(raw)
            try:
                raw.put_nowait(None)
            except Exception:
                pass
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=1.0)
        self._mic_raw = self._mic_worker = None
        if self._mic_queue is not None:
            nova_voice.drain(self._mic_queue)

    # ── subscriber fan-out ────────────────────────────────────────────────

    def _on_stall(self) -> None:
        """Speech went up and nothing came back. Say so, then reconnect.

        Called from the microphone callback, so it must not block: it
        publishes, logs, and asks the session loop to drop the connection.
        The reconnect path already exists -- it simply had nothing to trigger
        it, because a silent stall raises no exception.
        """
        message = self._watchdog.message()
        _log("[LIVE] %s", message)
        try:
            self._publish(LiveEvent("error", message=message, recoverable=True))
        except Exception:
            pass
        self._request_reconnect("no reply for %ds" % int(self._watchdog.waiting_for()))

    def _request_reconnect(self, reason: str) -> bool:
        """Replace the current connection. Safe from any thread; never blocks.

        Ending the mic sender (the old way) ended only the mic sender: the
        receiver kept waiting on the stuck socket and nothing drained the
        mic queue, so NOVA stayed mute for minutes after saying she was
        reconnecting.
        """
        self._drop_reason = reason
        ev, loop = self._drop_event, self._loop
        if ev is None or loop is None or loop.is_closed():
            return False
        try:
            loop.call_soon_threadsafe(ev.set)
            return True
        except RuntimeError:
            return False

    #: Reconnect this long before GoAway's deadline even if a turn is still going.
    GO_AWAY_MARGIN_S = 3.0

    def _on_go_away(self, go_away: Any) -> None:
        """Gemini will close this connection soon: plan a graceful swap."""
        left = _duration_s(getattr(go_away, "time_left", None))
        if left is None:
            left = 10.0
        self._go_away_deadline = time.monotonic() + max(0.5, left - self.GO_AWAY_MARGIN_S)
        _log("[LIVE] GoAway: connection ends in %.1fs; reconnecting at the next quiet moment%s",
             left, " (resumable)" if self._resume_handle else "")
        self._publish(LiveEvent("go_away", time_left_s=round(left, 1)))

    def _quiet(self) -> bool:
        """Nothing would be cut off by swapping the connection now."""
        return not self._tool_tasks and not self.speaking

    async def _go_away_watch(self) -> None:
        """After GoAway, swap connections between turns -- never mid-tool if avoidable."""
        while True:
            await asyncio.sleep(0.25)
            deadline = self._go_away_deadline
            if deadline is None:
                continue
            now = time.monotonic()
            if self._quiet():
                self._request_reconnect("GoAway: session time limit; resuming on a new connection")
                return
            if now >= deadline:
                if self._tool_tasks:
                    _log("[LIVE] GoAway deadline reached with %d tool call(s) running; their results may not reach the model",
                         len(self._tool_tasks))
                self._request_reconnect("GoAway deadline")
                return

    async def _drop_watch(self) -> None:
        """Ends the session's TaskGroup when a reconnect is requested."""
        await self._drop_event.wait()
        raise ReconnectRequested(self._drop_reason or "reconnect requested")

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
            # Asked to stop? Then the loop ending is the thing that was asked
            # for, not a failure.
            #
            # stop() ends the session by calling loop.stop(), which makes
            # run_until_complete raise "Event loop stopped before Future
            # completed" — and that was landing here and being published as a
            # fatal error on the way out of every single clean shutdown. The
            # orb turned red, the window reported a dead voice session, and
            # the state was set to ERROR underneath stop() as it was setting
            # CLOSED. Nothing was wrong; NOVA had simply been switched off.
            if not self._should_reconnect():
                _log("[LIVE] loop ended on shutdown: %s", e)
                return
            _log("[LIVE] loop died: %s", e)
            self._last_error = str(e)
            with self._state_lock:
                self._state = LiveState.ERROR
            self._publish(LiveEvent("state", state="error", error=str(e)))
        finally:
            self._drain_loop()

    def _drain_loop(self) -> None:
        """Cancel whatever is still pending before letting the loop go.

        stop() ends the session with loop.stop(), which leaves tasks suspended
        rather than cancelled -- the websocket keepalive and the receiver,
        which is parked on a Future that will never resolve. Python then
        reports each one as "Task was destroyed but it is pending!" at ERROR,
        and those were the only errors in an otherwise clean shutdown.

        Nothing here is allowed to raise: this runs on the way out, and a
        failure to tidy up must not become the last thing the session does.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            if not pending:
                return
            for task in pending:
                task.cancel()
            # Let the cancellations actually be delivered; without this the
            # tasks are cancelled but never resumed, which reports the same
            # way.
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True))
            _log("[LIVE] cancelled %d pending task(s) on shutdown", len(pending))
        except Exception as exc:
            _log("[LIVE] could not drain the session loop: %s", exc)

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
                or "errno 11002" in e
                # A connection that never completes is the same situation
                # wearing a different message. On 2026-09-21 an outage
                # produced six "timed out during opening handshake" in two
                # minutes; because those read as a refusal rather than an
                # outage, they burned the give-up budget and voice died
                # fifty-four seconds before the network came back.
                or "timed out during opening handshake" in e
                or "handshake operation timed out" in e
                or "timed out during handshake" in e
                or "connection timed out" in e)

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
        # The spoken variant of NOVA's personality (nova_personality): same
        # identity as text chat, shaped for the ear.
        sys_prompt = _resolve("NOVA_VOICE_PROMPT", "") or _resolve("NOVA_SYSTEM_PROMPT", "")
        identity = _identity_block()
        if identity:
            # Before background memory, because it outranks it: memory is what
            # NOVA has picked up, this is what she has been told.
            sys_prompt += f"\n\n{identity}"
        adaptation = _adaptation_block()
        if adaptation:
            # What this person has asked of NOVA (Layer B). After identity,
            # before memory: it shapes how she talks, not what she knows.
            sys_prompt += f"\n\n{adaptation}"
        if mem_ctx:
            sys_prompt += f"\n\n[BACKGROUND MEMORY]\n{mem_ctx}"
        tool_decls = _live_tool_declarations(_resolve("TOOL_DECLARATIONS", []))

        def _new_client():
            """A client per connection attempt, never a reused one.

            The client carries HTTP session state, and after a connection has
            died that state is stale: reconnecting through it can reattach to a
            socket the far end has already given up on, which looks from here
            like a session that is connected and silent. A working reference
            implementation of this same API makes the same call and says why in
            one line -- "fresh client on every reconnect, avoids stale HTTP
            session state" -- which matches the failure seen here: after a DNS
            blip NOVA went on reporting 'streaming', went on sending audio, and
            never heard anything again.
            """
            key = os.environ["GEMINI_API_KEY"]
            if key == "nova-cloud-session":
                # Managed model (signed-in account, no key on this machine):
                # the NOVA backend issues a short-lived Live token and the
                # audio goes straight to Google with it.
                from desk import creds as _creds
                tok = _creds.current_cloud_client().mint_live_token()
                c = genai.Client(api_key=tok["token"],
                                 http_options={"api_version": tok.get("api_version", "v1alpha")})
            else:
                c = genai.Client(api_key=key)
            _relax_websocket_keepalive(c)
            return c

        try:
            client = _new_client()
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
            # A sliding context window. Without it a Live session that
            # carries video is capped at a couple of minutes, and an
            # audio-only one fills its context within the hour; with it the
            # oldest turns are summarised away and the session runs on.
            # Accepted by gemini-3.1-flash-live-preview (measured 2026-09-24).
            context_window_compression=gtypes.ContextWindowCompressionConfig(
                sliding_window=gtypes.SlidingWindow()),
            speech_config=gtypes.SpeechConfig(
                voice_config=gtypes.VoiceConfig(
                    prebuilt_voice_config=gtypes.PrebuiltVoiceConfig(
                        voice_name=self._voice,
                    )
                ),
                # Tell it what language to expect.
                #
                # Left to guess, it guesses badly on an accent it was not
                # expecting: this user's English came back transcribed as
                # French — "Oui", "mais tu peux m'appeler" — which is not a
                # microphone problem or a model problem but an unanswered
                # question. A hint costs nothing and is overridable, because
                # the right answer is different for every user.
                language_code=_voice_language(),
            ),
        )

        # Reconnect policy. Without a bound, a permanent failure (bad key,
        # revoked quota, broken TLS trust) turned into an infinite 2s retry
        # loop that spammed the log forever and never told the user anything.
        consecutive_failures = 0
        while self._should_reconnect():
            _log("[LIVE] connecting to %s voice=%s ...", self._model, self._voice)
            t0 = time.time()
            requested_reconnect = False
            connected_ok = False
            try:
                # Rebuilt per attempt; see _new_client. The first attempt reuses
                # the one already made above, so a normal start costs nothing.
                if consecutive_failures or self._offline:
                    client = _new_client()
                # Resume the conversation the last connection was having.
                config.session_resumption = gtypes.SessionResumptionConfig(handle=self._resume_handle)
                if self._resume_handle:
                    _log("[LIVE] resuming the previous session")
                self._go_away_deadline = None
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

                    # Per-turn state from the last session, cleared before
                    # this one can be misled by it.
                    #
                    # `_turn_done_flag` is set when the model says a turn is
                    # over and cleared when playback of that turn drains. A
                    # session stopped between those two — which is what
                    # closing NOVA mid-sentence is — leaves it set, and it was
                    # never reset on connect, so the *next* session began
                    # believing a turn it had not started had already
                    # finished. Measured: on a second session an interruption
                    # played 21 chunks of the sentence it was supposed to
                    # stop, because the tail window is not armed for a turn
                    # the model has already finished.
                    #
                    # A reconnect is a new turn by definition, so nothing from
                    # before it can be the tail of an interrupted one either.
                    self._turn_done_flag = False
                    self._suppress_until = 0.0
                    self._suppressed_tail = 0
                    # Queues belong to this loop and must exist before the
                    # microphone callback can post to them.
                    self._mic_queue = asyncio.Queue(maxsize=MIC_QUEUE_FRAMES)
                    self._input_text_queue = asyncio.Queue()
                    # One frame deep on purpose: if a frame is still waiting
                    # when the next is captured, the waiting one is already
                    # out of date.
                    self._video_queue = asyncio.Queue(maxsize=1)
                    self._pending_vision = None
                    # A frame captured for a session that then died is a
                    # picture of a conversation that no longer exists, and a
                    # busy flag left set is a NOVA who will not look at
                    # anything again until she is restarted.
                    self._vision_busy = False
                    self._vision_busy_at = 0.0
                    self._vision_last_at = 0.0
                    self._gate.reset()
                    # A new connection owes nothing to the old one's clock.
                    self._watchdog.reset()
                    self._cancelled_tool_ids.clear()
                    self._drop_event = asyncio.Event()
                    tg.create_task(self._drop_watch())
                    tg.create_task(self._go_away_watch())

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
                if _is_requested_reconnect(e):
                    # Deliberate, not a failure: nothing to count, no red orb
                    # for something NOVA chose to do, and no backoff -- the
                    # user is waiting to be answered.
                    _log("[LIVE] replacing the connection: %s", self._drop_reason)
                    requested_reconnect = True
                else:
                    consecutive_failures = self._on_connection_error(
                        e, connected_ok, consecutive_failures)
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
            if requested_reconnect:
                delay = min(delay, 0.2)
            with self._state_lock:
                self._state = LiveState.CONNECTING
            self._publish(LiveEvent("state", state="connecting", retry_in_s=round(delay, 1)))
            await asyncio.sleep(delay)

    def _on_connection_error(self, e: BaseException, connected_ok: bool,
                             failures: int) -> int:
        """Log and report a connection that failed; returns the new count."""
        if not connected_ok:
            failures += 1
        # While offline the same error repeats every few seconds by design
        # (so voice resumes the moment the network does); it once filled the
        # log with hundreds of identical lines. Log it once per outage.
        if not (self._offline and self._is_offline(str(e))):
            _log(
                "[LIVE] connect/run error (failure %d/%d): %s",
                failures, self.MAX_RECONNECT_ATTEMPTS, e,
            )
        self._last_error = str(e)
        if self._is_auth_failure(str(e)):
            # Permanent: report it in the user's language and stop.
            self._auth_rejected = True
        elif self._is_offline(str(e)):
            # Not a failure to count. Wait it out and say so plainly,
            # rather than exhausting the retry budget while the user
            # is on a train.
            failures = 0
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
        return failures

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
            name = _known_name(meta)
            if name:
                say = (meta.get("user_name_pronunciation") or "").strip()
                how = (f' Their name is pronounced "{say}" — say it that way. '
                       if say else " ")
                prompt = (
                    f"The user just opened this session. Greet {name} right now, out loud, "
                    "in 1-2 short spoken sentences — warm, a little informal, varying your "
                    f"phrasing rather than reusing a stock line.{how}"
                    "End by asking what they need. Do not wait for them to speak first."
                )
            else:
                # Nobody has told her who this is. Guessing, or falling back to
                # "there" forever, both skip the one question that makes every
                # later greeting personal -- so ask it once, properly, and get
                # the spelling and the sound of it rather than an approximation
                # she will mispronounce for the rest of the relationship.
                prompt = (
                    "The user just opened this session and you do not know their name "
                    "yet. Greet them warmly in one short spoken sentence, say you don't "
                    "think you've caught their name, and ask what you should call them. "
                    "Ask them to spell it if it's unusual, and to say it once so you "
                    "know how it sounds. Keep it to two short sentences and do not "
                    "invent a name. Do not wait for them to speak first."
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

                    # Checked first: neither carries server_content, and the
                    # `if sc is None: continue` below used to skip them both.
                    # GoAway went unanswered until Gemini closed the socket
                    # itself (1008), mid-sentence or mid-tool -- a finished
                    # browser_control result was lost that way -- and every
                    # reconnect began a new session with no memory.
                    upd = getattr(msg, "session_resumption_update", None)
                    if upd is not None and getattr(upd, "resumable", False)                             and getattr(upd, "new_handle", None):
                        self._resume_handle = upd.new_handle
                    if getattr(msg, "go_away", None) is not None:
                        self._on_go_away(msg.go_away)

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
                        self._spawn_tool_calls(session, msg.tool_call)
                        continue

                    cancel = getattr(msg, "tool_call_cancellation", None)
                    if cancel is not None:
                        self._cancel_tool_calls(getattr(cancel, "ids", None))
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
                                self._watchdog.model_responded()
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
                            self._watchdog.user_spoke()
                            heard.append(text)
                            self._publish(LiveEvent("user_transcript", text=text))

                    if sc.output_transcription and sc.output_transcription.text:
                        text = sc.output_transcription.text.strip()
                        if text:
                            said.append(text)
                            self._publish(LiveEvent("nova_transcript", text=text))

                    if sc.interrupted:
                        # Logged separately from a local barge-in: this is
                        # Gemini deciding it heard the user, and an unexplained
                        # half-sentence is one of the two.
                        _log("[LIVE] the model reports its turn was interrupted")
                        # The model has acknowledged the cut and stopped
                        # generating. Anything of the old turn still queued is now
                        # stale, so drop it rather than playing a fragment of an
                        # answer the user has already moved on from.
                        self._play_generation += 1
                        nova_voice.drain(self._play_q)
                        # Nothing after this belongs to the interrupted turn,
                        # so stop discarding — the next audio is her answer to
                        # whatever the user interrupted her to say.
                        self._end_suppression("model confirmed the cut")
                        self._publish(LiveEvent("interrupted"))
                        self._turn_count += 1

                    if sc.turn_complete:
                        # A finished turn is an answer, even a silent one.
                        self._watchdog.model_responded()
                        self._turn_done_flag = True
                        self._turn_count += 1
                        self._end_suppression("turn ended")
                        self._publish(LiveEvent("turn_complete", turn_count=self._turn_count))

                        # The turn that just ended was NOVA saying she would
                        # look. Now show her.
                        #
                        # Sent here rather than from the tool because a turn
                        # cannot be interleaved: content pushed while the
                        # model is still speaking is either ignored or cuts
                        # the acknowledgement off mid-word. Waiting for the
                        # turn to complete is what makes "let me take a
                        # look — you're looking at a stack trace" one
                        # continuous reply instead of two colliding ones.
                        if self._pending_vision is not None:
                            await self._send_pending_vision(session)
                        # Offer the finished exchange to long-term memory, off
                        # the event loop — deciding what is worth keeping can
                        # involve a model call and must never delay audio.
                        turn_user, turn_nova = " ".join(heard), " ".join(said)
                        heard, said = [], []
                        if turn_user or turn_nova:
                            if not hasattr(self, "_voice_history"):
                                self._voice_history = {}
                            threading.Thread(
                                target=_archive_turn,
                                args=(self._voice_history, turn_user, turn_nova),
                                name="nova-voice-history", daemon=True).start()
                        if turn_user:
                            threading.Thread(
                                target=_remember_turn, args=(turn_user, turn_nova),
                                name="nova-memory", daemon=True).start()
                            _learn_preferences(turn_user)


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

    async def _send_pending_vision(self, session: Any) -> None:
        """Put the captured screen in front of the model, as a question.

        `turn_complete=True`, unlike the ambient sampler, which sends frames
        as pending context with `turn_complete=False` so NOVA does not
        announce what she can see every two seconds. This frame *is* the
        question — the user asked it and is waiting for the answer — so it
        closes the turn and the model replies.
        """
        pending = self._pending_vision
        self._pending_vision = None
        if pending is None:
            return
        jpeg, mime, question = pending
        try:
            await session.send_client_content(
                turns={"role": "user", "parts": [
                    {"inline_data": {"mime_type": mime, "data": jpeg}},
                    {"text": question},
                ]},
                turn_complete=True)
            _log("[LIVE] screen sent to the model (%.0f kB)", len(jpeg) / 1024)
            self._publish(LiveEvent("vision_sent",
                                    kb=round(len(jpeg) / 1024, 1)))
        except Exception as e:
            # Losing the picture must not lose the conversation, but NOVA
            # cannot silently fail to look at something she said she would
            # look at — so the model is told, in the same turn, and says so.
            _log("[LIVE] could not send the screen: %s", e)
            self._publish(LiveEvent("vision_failed", error=str(e)))
            try:
                await session.send_client_content(
                    turns={"role": "user", "parts": [{"text": (
                        "The screen capture could not be delivered. Tell the "
                        "user you were unable to see their screen this time.")}]},
                    turn_complete=True)
            except Exception:
                pass
        finally:
            self._vision_busy = False

    def _spawn_tool_calls(self, session: Any, tool_call: Any) -> None:
        """Run a tool call beside the conversation instead of in front of it.

        The receiver used to await every tool, so for as long as one ran --
        a 9 s search, a confirmation waiting on the user -- nothing was read
        from the connection: no audio, no transcript, no interruption. The
        receiver must never wait on anything but the socket.
        """
        self._watchdog.tool_started()
        task = asyncio.get_running_loop().create_task(
            self._handle_tool_calls(session, tool_call))
        self._tool_tasks.add(task)
        task.add_done_callback(self._tool_tasks.discard)

    def _cancel_tool_calls(self, ids: Any) -> None:
        """Gemini withdrew these calls (usually: the user talked over them)."""
        ids = [i for i in (ids or []) if i]
        if ids:
            self._cancelled_tool_ids.update(ids)
            _log("[LIVE] tool call(s) withdrawn by the model: %s", ", ".join(ids))

    async def _handle_tool_calls(self, session: Any, tool_call: Any) -> None:
        """Run the tools the model asked for and send the results back."""
        try:
            await self._answer_tool_calls(session, tool_call)
        finally:
            self._watchdog.tool_finished()

    async def _answer_tool_calls(self, session: Any, tool_call: Any) -> None:
        names = [fc.name for fc in tool_call.function_calls]
        _log("[LIVE] tool call: %s", ", ".join(names))
        self._publish(LiveEvent("tool_call", tools=names))

        responses = []
        for fc in tool_call.function_calls:
            responses.append(await self._run_tool(fc))

        def _rid(r):
            return r.get("id") if isinstance(r, dict) else getattr(r, "id", None)
        responses = [r for r in responses if _rid(r) not in self._cancelled_tool_ids]
        if not responses:
            return

        try:
            await session.send_tool_response(function_responses=responses)
        except Exception as e:
            # The tool ran; the model never heard about it and will stall.
            # Say so rather than leaving a silent NOVA to explain.
            _log("[LIVE] send_tool_response failed: %s", e)
            self._publish(LiveEvent("error", error="tool_response_failed",
                                    message=str(e)))

    #: Vision arguments the live session genuinely cannot serve.
    #:
    #: Deliberately short. Saving writes a file and face work runs local
    #: models against the webcam, so both belong to NOVA Core. Reading text
    #: does not: the live model reads a screenshot perfectly well, and
    #: sending "read this" to Core meant a second model over REST with the
    #: conversation blocked behind it — which is the whole failure this
    #: routing exists to avoid. It was also the common case, because "read
    #: my screen" is how people ask.
    _VISION_NEEDS_CORE = ("save", "save_reference", "identify_user")

    #: ...and these, but only when there is a face to look for.
    #:
    #: detect_faces used to send every call to Core unconditionally, and the
    #: model sets it freely: "what is on my screen?" arrived with
    #: detect_faces=True and went to a 10.7 s REST round trip to count faces
    #: on a desktop. Counting faces in a screenshot is not a question anyone
    #: asked. It is a camera operation, and only there is it worth the wait.
    _VISION_CAMERA_ONLY = ("detect_faces",)

    #: Shortest gap between two captures NOVA will honour.
    #:
    #: The model does not ask once. Observed in one session: twenty-plus
    #: `vision` calls back to back, frequently two in a single tool_call
    #: block, each Core-routed one costing 10.7 s — minutes of a conversation
    #: spent photographing an unchanged screen. It had already been given an
    #: answer and asked again anyway.
    #:
    #: Four seconds is the reference implementation's figure and it is a good
    #: one: longer than any turn in which a second look could be meaningful,
    #: short enough that a genuine follow-up ("now look again") is not
    #: refused. Within it the model is told it already has the image, which
    #: is both true and the thing that stops it asking a third time.
    VISION_COOLDOWN_S = 4.0

    #: A capture that never reached the model must not lock the fast path out
    #: for the rest of the session. If the acknowledging turn never completes
    #: — a barge-in, a reconnect mid-turn — the flag is stale, and every later
    #: request silently falls back to the blocking path.
    VISION_BUSY_MAX_S = 20.0

    #: Where a frame can come from without leaving the conversation.
    _VISION_SOURCES = ("screen", "camera")

    def _vision_too_soon(self, fc: Any) -> Any:
        """Refuse a repeat look, or None to allow it.

        Applied before routing, because the storm is not about which path a
        call takes — it is about there being twenty of them. A refusal that
        returns instantly is worth far more to the conversation than a
        correct answer that arrives after four more captures.

        The refusal says the image is already there, because it is: the model
        was sent one seconds ago and is asking again rather than answering
        from it.
        """
        now = time.monotonic()
        since = now - self._vision_last_at
        if self._vision_last_at and since < self.VISION_COOLDOWN_S:
            _log("[LIVE] vision refused: asked again %.1fs after the last look",
                 since)
            self._publish(LiveEvent("vision_refused", since_s=round(since, 1)))
            return gtypes.FunctionResponse(
                id=fc.id, name=fc.name,
                response={"output": (
                    "You have already been sent an image of this, moments ago. "
                    "Do NOT call vision again — answer the user's question from "
                    "the image you already have.")})
        self._vision_last_at = now
        return None

    def _vision_in_session(self, args: dict) -> bool:
        """Can this 'vision' call be answered by showing the model a picture?

        Logged either way. Which path a vision request takes is the difference
        between a one-second answer and a thirty-second silence, and there was
        no way to tell from the outside which one had been chosen.
        """
        why = ""
        angle = str(args.get("angle", "screen") or "screen").lower()
        if angle not in self._VISION_SOURCES:
            why = f"angle={angle!r} is not something we can capture"
        elif any(args.get(k) for k in self._VISION_NEEDS_CORE):
            why = "needs " + ", ".join(k for k in self._VISION_NEEDS_CORE
                                       if args.get(k))
        elif angle == "camera" and any(args.get(k) for k in self._VISION_CAMERA_ONLY):
            why = "needs " + ", ".join(k for k in self._VISION_CAMERA_ONLY
                                       if args.get(k))
        elif self._vision_busy:
            if time.monotonic() - self._vision_busy_at > self.VISION_BUSY_MAX_S:
                _log("[LIVE] vision busy flag was stale (%.0fs); clearing it",
                     time.monotonic() - self._vision_busy_at)
                self._vision_busy = False
            else:
                why = "a capture is already in flight"
        if why:
            _log("[LIVE] vision -> Core (%s); args=%s", why, sorted(args))
            return False
        _log("[LIVE] vision -> live session; args=%s", sorted(args))
        return True

    @staticmethod
    def _capture_for(source: str) -> tuple[bytes, str]:
        """One frame from `source`, in memory, ready to send."""
        if source == "camera":
            # Core owns the webcam: index, warm-up frames and compression are
            # all settled there, and duplicating that here would mean two
            # answers to "which camera".
            from actions.screen_processor import _capture_camera
            return _capture_camera(), "image/jpeg"
        return capture_once()

    async def _look(self, fc: Any, args: dict) -> Any:
        """Answer "what's on my screen?" inside the conversation, not beside it.

        The alternative — and what this replaces — was Core's vision tool: it
        captures the screen, writes it to disk, and asks a *second* model
        about it over REST, then hands the answer back as a string for the
        live model to read out. That is a whole extra round trip on its own
        quota, a picture of the user's screen on their filesystem, and several
        seconds of complete silence while it happens, because a tool call
        blocks the turn. The user asks NOVA to look at something and she
        appears to have stopped working.

        So the frame goes to the model that is already in the conversation.
        The tool returns immediately with an instruction to say something
        while the looking happens, and the image is sent the moment that
        acknowledgement finishes — as its own turn, so the model answers the
        question rather than filing the picture away as context. The screen
        NOVA describes is then the screen as it was when asked about, seen by
        the model that heard the question, in one round trip.

        Notably not the ambient sampler: that path exists so the screen is
        already in context when someone asks, and it is throttled and
        deliberately low-resolution for it. This one was asked for.
        """
        source = str(args.get("angle", "screen") or "screen").lower()
        default_q = ("What can you see through my camera?" if source == "camera"
                     else "What is on my screen?")
        question = str(args.get("question") or default_q).strip()
        if args.get("ocr_only"):
            question = ("Read out all the text visible in this image, exactly "
                        "as written. " + question)
        self._vision_busy = True
        self._vision_busy_at = time.monotonic()
        self._publish(LiveEvent("vision_capture", source=source))
        try:
            jpeg, mime = await asyncio.to_thread(self._capture_for, source)
        except Exception as e:
            self._vision_busy = False
            _log("[LIVE] %s capture failed: %s", source, e)
            self._publish(LiveEvent("vision_failed", source=source, error=str(e)))
            return gtypes.FunctionResponse(
                id=fc.id, name=fc.name,
                response={"output": f"NOVA could not use the {source}: {e}"})

        self._pending_vision = (jpeg, mime, question)
        _log("[LIVE] %s captured for '%s' (%.0f kB)", source, question[:60],
             len(jpeg) / 1024)
        self._publish(LiveEvent("vision_captured", source=source,
                                kb=round(len(jpeg) / 1024, 1),
                                question=question[:120]))
        what = "camera" if source == "camera" else "screen"
        return gtypes.FunctionResponse(
            id=fc.id, name=fc.name,
            response={"output": (
                f"The {what} has been captured and is being sent to you now. "
                "Say one short natural sentence to let the user know you are "
                "looking — nothing more. Do not describe or guess what you can "
                f"see: the image arrives in the very next message, and you "
                "will answer their question from it then.")})

    async def _run_tool(self, fc: Any) -> Any:
        """Execute one tool call off the event loop.

        Tools open applications, drive the browser and touch the filesystem —
        all of it blocking, and none of it allowed to stall the audio stream
        that is running on this loop.
        """
        name = fc.name
        args = dict(fc.args or {})
        if name == "vision":
            refusal = self._vision_too_soon(fc)
            if refusal is not None:
                return refusal
            if self._vision_in_session(args):
                return await self._look(fc, args)
        t0 = time.time()
        budget = VISION_TIMEOUT_S if name == "vision" else TOOL_TIMEOUT_S
        # The agent that owns this tool is working until it answers -- the
        # Agents panel reads this, whichever path (voice, chat, task) ran it.
        work = agent_activity.begin(agent_activity.agent_for_tool(name),
                                    _tool_action(name, args), source="voice")
        try:
            result = await self._await_tool(
                asyncio.ensure_future(asyncio.to_thread(self._execute_tool, name, args)),
                budget)
        except asyncio.TimeoutError:
            # Say it failed, and say not to try again.
            #
            # The old wording — "it may still be running" — read as an
            # invitation to retry, and the model took it: four vision calls
            # back to back, each timing out at thirty seconds, two full
            # minutes during which NOVA said nothing at all. From the user's
            # side that is an assistant that has stopped working.
            result = (f"The '{name}' tool did not respond within "
                      f"{budget:.0f} seconds and has been given up on. Do NOT "
                      f"call it again for this request. Tell the user plainly "
                      f"that it did not work this time, and carry on.")
            _log("[LIVE] tool %s timed out after %.0fs", name, budget)
        except Exception as e:
            result = f"Tool '{name}' encountered an error: {str(e)[:200]}"
            _log("[LIVE] tool %s failed: %s", name, e)
        finally:
            agent_activity.end(work)
        _log("[LIVE] tool %s finished in %.1fs", name, time.time() - t0)
        self._publish(LiveEvent("tool_result", tool=name,
                                summary=str(result)[:200]))
        response = result if isinstance(result, dict) else {"output": str(result)}
        if name in NON_BLOCKING_TOOLS:
            # She kept talking while this ran; let her finish her sentence.
            return gtypes.FunctionResponse(
                id=fc.id, name=name, response=response,
                scheduling=gtypes.FunctionResponseScheduling.WHEN_IDLE)
        return gtypes.FunctionResponse(id=fc.id, name=name, response=response)

    @staticmethod
    async def _await_tool(task: "asyncio.Future", budget: float) -> Any:
        """Wait for a tool, but not against the person.

        The budget is for the tool's own work. While NOVA is waiting for the
        person to answer "should I proceed?", the clock stops -- it used to
        keep running, so a read that needed a yes gave up at 30 s while the
        question was still on screen, and the "yes" arrived for nothing. The
        confirmation store has its own limit (five minutes), after which it
        answers "no" itself.
        """
        deadline = time.time() + budget
        while True:
            done, _ = await asyncio.wait({task}, timeout=min(1.0, max(0.05, deadline - time.time())))
            if done:
                return task.result()
            if _confirmation_waiting():
                deadline = max(deadline, time.time() + 5.0)
            elif time.time() >= deadline:
                raise asyncio.TimeoutError()

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
        """Stream the screen to the model on the realtime video channel.

        Real time, as ambient mode needs: up to one frame a second while the
        screen is changing. Measured 2026-09-24 against
        gemini-3.1-flash-live-preview, with frames sent this way and the
        question asked by voice ("what can you see on my screen right now?")
        the answer described the desktop correctly. (An older model ignored
        this channel, which is why frames used to go as client content.)

        Client content was also the wrong vehicle for a stream: every frame
        became a permanent piece of the conversation, so frames had to be
        rationed to one per turn -- up to ten seconds stale -- or the context
        would fill with pictures of the desktop. Realtime input is a stream,
        opens no turn (she does not announce what she sees), and the session
        slides its context window (see context_window_compression).
        """
        last_sent_at = 0.0
        while True:
            try:
                item = await self._video_queue.get()
                if item is None:
                    break
                jpeg, mime = item

                # At most one frame a second: the rate the realtime video
                # channel is built for. (ScreenShare already paces capture;
                # this holds even if something else offers frames.)
                now = time.monotonic()
                if now - last_sent_at < SCREEN_STREAM_MIN_S:
                    continue

                # Never push a picture through a socket that is already
                # struggling to carry the conversation.
                #
                # A frame is around 32 KB against 32 KB/s of microphone audio,
                # so on a link with little headroom one screenshot is most of
                # a second during which the user's voice is queueing behind
                # it. Measured on a real session: 588 KB of frames, mic sends
                # climbing to twenty seconds, and 180 frames of speech thrown
                # away because the queue behind them filled up.
                #
                # The screen is context; the voice is the conversation. When
                # there is not room for both, the picture waits.
                backlog = self._mic_queue.qsize() if self._mic_queue else 0
                if backlog > MIC_QUEUE_FRAMES // 4:
                    _log("[LIVE] holding screen frame: %d mic frames queued",
                         backlog)
                    continue

                await session.send_realtime_input(
                    video=gtypes.Blob(data=jpeg, mime_type=mime))
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
        """Send text into the conversation, always as a completed turn.

        `turn_complete=False` was tried for the interruption notice, to stop
        her answering "okay" the moment she was asked for silence. It is the
        wrong tool: content sent that way does not cancel the generation in
        progress, so the model carried on producing the very answer that had
        just been stopped. Measured — the local window suppressed the first
        three seconds and then 45 chunks of the old answer played out of it.

        A completed turn is what cancels generation, and it does so in about
        two tenths of a second. That is the whole point of telling her.
        """
        while True:
            try:
                text = await self._input_text_queue.get()
                if text is None:
                    break
                _log("[LIVE] sending typed text (%d chars); mic_active=%s",
                     len(text), self._mic_active)
                await session.send_client_content(
                    turns={"role": "user", "parts": [{"text": text}]},
                    turn_complete=True,
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                # A failed send used to kill this whole loop -- the queue
                # kept accepting new text from send_text() (it only pushes
                # onto _input_text_queue), but nothing was left running to
                # ever consume it, so every message typed after the first
                # failure silently vanished for the rest of the session.
                # One bad send is a reason to log and keep listening for
                # the next one, not a reason to stop being able to type at
                # all.
                _log("[LIVE] text send error (will keep accepting more): %s", e)
                continue


# ── module singleton ──────────────────────────────────────────────────────────
_manager: LiveManager | None = None


def get_live_manager() -> LiveManager:
    global _manager
    if _manager is None:
        _manager = LiveManager()
    return _manager
