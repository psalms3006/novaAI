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
from enum import Enum
from typing import Any

import numpy as np

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
LIVE_RATE = 24000  # Gemini Live outputs 24 kHz 16-bit mono PCM


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
        self._mic_queue: queue.Queue = queue.Queue(maxsize=50)
        self._input_text_queue: queue.Queue = queue.Queue()
        self._t_first_audio: float = 0.0
        self._t_connected: float = 0.0
        self._start_time: float = 0.0
        self._last_error: str = ""
        self._turn_count: int = 0
        self._audio_bytes_out: int = 0
        self._audio_bytes_in: int = 0

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
        self._thread = threading.Thread(target=self._run_loop, name="nova-live", daemon=True)
        self._thread.start()
        return {"ok": True, "message": "connecting"}

    def stop(self) -> dict:
        with self._state_lock:
            if self._state in (LiveState.IDLE, LiveState.CLOSED):
                return {"ok": True, "message": "not running"}
            self._state = LiveState.DISCONNECTING
        self._publish(LiveEvent("state", state="disconnecting"))
        # Send sentinel to unblock mic sender, then stop loop
        self._mic_queue.put(None)
        self._input_text_queue.put(None)
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=3.0)
        self._stop_mic()
        self._session = None
        with self._state_lock:
            self._state = LiveState.CLOSED
        self._publish(LiveEvent("state", state="closed"))
        return {"ok": True, "message": "stopped"}

    def send_text(self, text: str) -> dict:
        with self._state_lock:
            if self._state not in (LiveState.CONNECTED, LiveState.STREAMING):
                return {"ok": False, "reason": "not connected"}
        self._input_text_queue.put(text)
        return {"ok": True}

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

    def _start_mic(self) -> None:
        if not HAS_SD or self._mic_active:
            return

        def _cb(indata, frames, time_info, status):
            try:
                self._mic_queue.put_nowait(bytes(indata))
            except queue.Full:
                pass

        try:
            self._mic_stream = sd.InputStream(
                samplerate=MIC_RATE, channels=1, dtype="int16",
                blocksize=1024, callback=_cb,
            )
            self._mic_stream.start()
            self._mic_active = True
            _log("[LIVE] mic opened (16 kHz mono)")
        except Exception as e:
            _log("[LIVE] mic open failed: %s", e)

    def _stop_mic(self) -> None:
        self._mic_active = False
        if self._mic_stream is not None:
            try:
                self._mic_stream.stop()
                self._mic_stream.close()
            except Exception:
                pass
            self._mic_stream = None
        while not self._mic_queue.empty():
            try:
                self._mic_queue.get_nowait()
            except queue.Empty:
                break

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

    async def _connect_and_run(self) -> None:
        meta = _load_meta()
        mem_ctx = _build_memory_context(meta)
        sys_prompt = _resolve("NOVA_SYSTEM_PROMPT", "")
        if mem_ctx:
            sys_prompt += f"\n\n[BACKGROUND MEMORY]\n{mem_ctx}"
        tool_decls = _resolve("TOOL_DECLARATIONS", [])

        try:
            client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
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
                    dt = time.time() - t0
                    self._t_connected = time.time()
                    with self._state_lock:
                        self._state = LiveState.STREAMING
                    _log("[LIVE] connected in %.1fs", dt)
                    self._publish(LiveEvent("state", state="connected", connect_time_s=round(dt, 2)))

                    self._start_mic()

                    # Auto-send greeting (like terminal behavior)
                    await self._send_greeting(session)

                    tg.create_task(self._mic_sender(session))
                    tg.create_task(self._receiver(session))
                    tg.create_task(self._text_sender(session))
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
                self._publish(LiveEvent("state", state="error", error=str(e)))
            finally:
                self._stop_mic()
                self._session = None

            # Reconnect on transient drops (keepalive timeout, network blip, 5xx)
            if not self._should_reconnect():
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
            # a trickle of retries rather than 30 per minute.
            delay = min(
                self.RECONNECT_BASE_DELAY * (2 ** max(0, consecutive_failures - 1)),
                self.RECONNECT_MAX_DELAY,
            )
            with self._state_lock:
                self._state = LiveState.CONNECTING
            self._publish(LiveEvent("state", state="connecting", retry_in_s=round(delay, 1)))
            await asyncio.sleep(delay)

    async def _send_greeting(self, session: Any) -> None:
        """Send a proactive greeting after connecting, like the terminal does."""
        await asyncio.sleep(0.5)  # let receiver start consuming first
        meta = _load_meta()
        user_name = (meta.get("user_name") or "").strip()
        name_part = user_name if user_name else "there"
        prompt = (
            f"The user just opened this desktop session. Greet {name_part} right now, out loud, "
            "in 1-2 short spoken sentences — warm, natural, helpful. "
            "Vary your phrasing. End by asking what you can help with. "
            "Do not wait for them to speak first. Speak immediately."
        )
        try:
            await session.send_client_content(
                turns={"role": "user", "parts": [{"text": prompt}]}, turn_complete=True
            )
            _log("[LIVE] greeting sent")
        except Exception as e:
            _log("[LIVE] greeting failed: %s", e)

    async def _mic_sender(self, session: Any) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                data = await loop.run_in_executor(None, self._mic_queue.get, True, 0.1)
                if data is None:
                    break
                await session.send_realtime_input(
                    audio=gtypes.Blob(data=data, mime_type="audio/pcm;rate=16000")
                )
                self._audio_bytes_in += len(data)
            except queue.Empty:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                _log("[LIVE] mic send error: %s", e)
                raise

    async def _receiver(self, session: Any) -> None:
        try:
            async for msg in session.receive():
                sc = msg.server_content
                if sc is None:
                    continue

                if sc.model_turn and sc.model_turn.parts:
                    for part in sc.model_turn.parts:
                        inline = getattr(part, "inline_data", None)
                        if inline is not None and getattr(inline, "data", None):
                            audio = inline.data
                            b64 = base64.b64encode(audio).decode("ascii")
                            self._audio_bytes_out += len(audio)
                            if self._t_first_audio == 0.0:
                                self._t_first_audio = time.time()
                            self._publish(LiveEvent("audio", data_b64=b64, size=len(audio)))

                if sc.input_transcription and sc.input_transcription.text:
                    text = sc.input_transcription.text.strip()
                    if text:
                        self._publish(LiveEvent("user_transcript", text=text))

                if sc.output_transcription and sc.output_transcription.text:
                    text = sc.output_transcription.text.strip()
                    if text:
                        self._publish(LiveEvent("nova_transcript", text=text))

                if sc.interrupted:
                    self._publish(LiveEvent("interrupted"))
                    self._turn_count += 1

                if sc.turn_complete:
                    self._turn_count += 1
                    self._publish(LiveEvent("turn_complete", turn_count=self._turn_count))

                if msg.go_away:
                    self._publish(LiveEvent("go_away"))
                    raise ConnectionError("gemini live go_away")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            _log("[LIVE] receiver error: %s", e)
            self._publish(LiveEvent("error", error=str(e)))
            raise

    async def _text_sender(self, session: Any) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                text = await loop.run_in_executor(None, self._input_text_queue.get, True, 0.1)
                if text is None:
                    break
                await session.send_client_content(
                    turns={"role": "user", "parts": [{"text": text}]}, turn_complete=True
                )
            except queue.Empty:
                continue
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
