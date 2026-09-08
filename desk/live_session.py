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

import nova_voice

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
# If 'speaking' is set the mic is muted, so a stuck flag means NOVA is deaf.
# Longest plausible gap between audio chunks inside one model turn.
SPEAKING_WATCHDOG_S = 3.0
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
        self._has_greeted: bool = False

        # One shared voice policy — identical rules to the terminal path.
        self._gate = nova_voice.VoiceGate(
            chunk_samples=1024, on_barge_in=self._barge_in,
        )
        # NOVA Core owns playback. The UI surfaces receive amplitude/state and
        # only visualise it, so the fullscreen window and the ambient orb can
        # never play the same audio twice.
        self._play_q: queue.Queue = queue.Queue(maxsize=200)
        self._play_thread: threading.Thread | None = None
        self._play_stop = threading.Event()
        self._turn_done_flag = False
        # Barge-in suppression, scoped to the turn it belongs to.
        #
        # This was a plain boolean latch and that made NOVA go deaf: it was
        # cleared only by `interrupted`/`turn_complete` from the model, but a
        # barge-in on the *tail* of playback happens after the model already
        # finished the turn, so neither message ever arrives again. The latch
        # stayed set and every later chunk was discarded -- mic streaming,
        # model replying, NOVA silent.
        #
        # An epoch fixes it: suppression only applies to the turn that was
        # actually interrupted, and any new turn -- including one detected
        # purely from the user starting to speak -- moves past it.
        self._turn_epoch = 0
        self._interrupted_epoch = -1
        self._speaker_alive = False
        self._last_audio_at = 0.0

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

    # ── playback (Core-owned) ─────────────────────────────────────────────

    def _start_playback(self) -> None:
        if self._play_thread is not None or not HAS_SD:
            return
        self._play_stop.clear()

        def _worker():
            try:
                stream = sd.RawOutputStream(
                    samplerate=nova_voice.RECEIVE_RATE,
                    channels=nova_voice.CHANNELS,
                    dtype="int16",
                    blocksize=4096,
                    latency="low",
                )
                stream.start()
            except Exception as e:
                _log("[LIVE] speaker open failed: %s", e)
                # Make sure a dead speaker never leaves the mic muted.
                self._speaker_alive = False
                self._gate.set_speaking(False)
                return
            self._speaker_alive = True
            _log("[LIVE] speaker ready (%d Hz)", nova_voice.RECEIVE_RATE)
            try:
                while not self._play_stop.is_set():
                    try:
                        chunk = self._play_q.get(timeout=0.2)
                    except queue.Empty:
                        # Queue drained and the model finished its turn: NOVA
                        # has stopped talking, so reopen the mic.
                        if self._gate.speaking and self._turn_done_flag:
                            self._gate.set_speaking(False)
                            self._turn_done_flag = False
                            self._publish(LiveEvent("state", state="listening"))
                        elif self._gate.speaking and self._last_audio_at:
                            # Watchdog. 'speaking' mutes the mic, so anything
                            # that leaves it set is deafness. If the queue has
                            # been empty well past any plausible gap in the
                            # model's audio, stop believing NOVA is talking.
                            if time.time() - self._last_audio_at > SPEAKING_WATCHDOG_S:
                                _log("[LIVE] watchdog: clearing stuck speaking state")
                                self._gate.set_speaking(False)
                                self._turn_done_flag = False
                                self._publish(LiveEvent("state", state="listening"))
                        continue
                    if chunk is None:
                        break
                    try:
                        stream.write(chunk)
                    except Exception as e:
                        _log("[LIVE] speaker write failed: %s", e)
            finally:
                self._speaker_alive = False
                self._gate.set_speaking(False)
                try:
                    stream.stop(); stream.close()
                except Exception:
                    pass

        self._play_thread = threading.Thread(target=_worker, name="nova-speaker", daemon=True)
        self._play_thread.start()

    def _stop_playback(self) -> None:
        self._play_stop.set()
        try:
            self._play_q.put_nowait(None)
        except Exception:
            pass
        self._play_thread = None

    def _enqueue_audio(self, audio: bytes) -> None:
        """Play a chunk and tell the surfaces how loud it is."""
        if self._interrupted_epoch == self._turn_epoch:
            # The user cut into *this* turn. Discard the rest of it instead of
            # resuming playback and immediately re-triggering the detector.
            # Scoped to the epoch, so it cannot leak into the next turn.
            return
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
        """User spoke over NOVA — stop immediately and stay stopped."""
        # Only suppress a turn that is genuinely still in flight. A cough on
        # the *tail* of playback -- after the model already finished the turn
        # -- has nothing to cut off, and latching there is what made NOVA
        # silent for every turn afterwards.
        turn_in_flight = (not self._turn_done_flag) and (
            self._play_q.qsize() > 0
            or (self._last_audio_at and time.time() - self._last_audio_at < 1.0)
        )
        if turn_in_flight:
            self._interrupted_epoch = self._turn_epoch
        # Clear speaking here too, not just in VoiceGate.process. "Stop
        # talking" has to be self-sufficient: any caller of _barge_in should
        # leave NOVA genuinely stopped, not rely on the detector having
        # already done half the work.
        self._gate.set_speaking(False)
        dropped = nova_voice.drain(self._play_q)
        _log("[LIVE] barge-in — dropped %d queued chunks", dropped)
        self._publish(LiveEvent("interrupted", reason="barge_in"))
        self._publish(LiveEvent("state", state="listening"))

    def set_muted(self, value: bool) -> dict:
        self._gate.set_muted(bool(value))
        self._publish(LiveEvent("state", state="muted" if value else "listening"))
        return {"ok": True, "muted": self._gate.muted}

    @property
    def muted(self) -> bool:
        return self._gate.muted

    def _start_mic(self) -> None:
        if not HAS_SD or self._mic_active:
            return

        def _cb(indata, frames, time_info, status):
            # Every rule about what reaches the model lives in nova_voice:
            # mute-while-speaking, silence keepalive, barge-in detection.
            try:
                payload = self._gate.process(np.asarray(indata).reshape(-1))
                self._mic_queue.put_nowait(payload)
            except queue.Full:
                pass
            except Exception:
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
                    self._start_playback()

                    # Greet once per session, not on every reconnect. The Live
                    # socket drops on keepalive timeout roughly every minute of
                    # silence, so greeting on reconnect made an idle NOVA speak
                    # an unprompted greeting over and over.
                    if not self._has_greeted:
                        self._has_greeted = True
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
                self._stop_playback()
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
        """Say something worth saying — or just say hello.

        The greeting is NOVA Core's, not the session's. Core is asked whether it
        has anything genuinely worth surfacing (missed notices, system events);
        if it does, that becomes the opening line, otherwise NOVA gives a short
        natural greeting. Either way she then goes straight back to listening.
        """
        await asyncio.sleep(0.5)  # let the receiver start consuming first

        surfaced = ""
        try:
            import nova_core_voice
            surfaced = nova_core_voice.opening_line(_load_meta()) or ""
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
            await session.send_client_content(
                turns={"role": "user", "parts": [{"text": prompt}]}, turn_complete=True
            )
            _log("[LIVE] opening sent (%s)", "proactive" if surfaced else "greeting")
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
                            self._audio_bytes_out += len(audio)
                            if self._t_first_audio == 0.0:
                                self._t_first_audio = time.time()
                            # NOVA Core owns playback. Previously the raw PCM
                            # was shipped to the browser, which meant any
                            # surface with the page open played it — with the
                            # fullscreen window and the ambient orb both open
                            # that is the same audio twice.
                            self._enqueue_audio(audio)

                if sc.input_transcription and sc.input_transcription.text:
                    text = sc.input_transcription.text.strip()
                    if text:
                        # The user is speaking, so whatever turn was
                        # interrupted is over. This is the backstop that makes
                        # deafness impossible even if the model never sends
                        # `interrupted` or `turn_complete` again.
                        if self._interrupted_epoch == self._turn_epoch:
                            self._turn_epoch += 1
                        self._publish(LiveEvent("user_transcript", text=text))

                if sc.output_transcription and sc.output_transcription.text:
                    text = sc.output_transcription.text.strip()
                    if text:
                        self._publish(LiveEvent("nova_transcript", text=text))

                if sc.interrupted:
                    self._turn_epoch += 1            # model acknowledged the cut
                    self._publish(LiveEvent("interrupted"))
                    self._turn_count += 1

                if sc.turn_complete:
                    self._turn_done_flag = True
                    self._turn_epoch += 1            # next turn may speak again
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
