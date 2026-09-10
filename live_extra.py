"""live_extra.py — NOVALive + Gemini chat/STT helpers, extracted from nova.py (Phase 2)."""
from __future__ import annotations
import asyncio
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import numpy as np

import nova_voice
import sounddevice as sd

# ══════════════════════════════════════════════════════════════════════════════
#  DIAGNOSTIC INSTRUMENTATION — additive only, no behavior change.
#  Set NOVA_DIAG=0 to silence. Every line is timestamped and stage-tagged so
#  failures can be traced to the exact stage in the pipeline that broke.
# ══════════════════════════════════════════════════════════════════════════════
_DIAG_ON = os.getenv("NOVA_DIAG", "1") != "0"

def _diag(stage: str, msg: str) -> None:
    if not _DIAG_ON:
        return
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[DIAG {ts}] [{stage}] {msg}", flush=True)

import nova as _nova
log = _nova.log
HAS_GEMINI = _nova.HAS_GEMINI
GEMINI_API_KEY = _nova.GEMINI_API_KEY
genai = _nova.genai if HAS_GEMINI else None
gtypes = _nova.gtypes if HAS_GEMINI else None
LIVE_MODEL = _nova.LIVE_MODEL
VISION_MODEL = _nova.VISION_MODEL
NOVA_VOICE = _nova.NOVA_VOICE
NOVA_SYSTEM_PROMPT = _nova.NOVA_SYSTEM_PROMPT
TOOL_DECLARATIONS = _nova.TOOL_DECLARATIONS
SEND_SAMPLE_RATE = _nova.SEND_SAMPLE_RATE
RECEIVE_SAMPLE_RATE = _nova.RECEIVE_SAMPLE_RATE
CHANNELS = _nova.CHANNELS
CHUNK_SIZE = _nova.CHUNK_SIZE
MAX_GEMINI_RETRIES = _nova.MAX_GEMINI_RETRIES
MAX_HISTORY_TURNS = _nova.MAX_HISTORY_TURNS
_MEM_EXTRACT_EVERY_N = _nova._MEM_EXTRACT_EVERY_N
DEFAULT_THRESHOLD = _nova.DEFAULT_THRESHOLD
HAS_FASTER_WHISPER = _nova.HAS_FASTER_WHISPER
WhisperModel = _nova.WhisperModel if HAS_FASTER_WHISPER else None
WHISPER_MODEL_SIZE = _nova.WHISPER_MODEL_SIZE
_stt_loaded = _nova._stt_loaded
_stt_model_lock = _nova._stt_model_lock
_executor = _nova._executor
_execute_tool_sync = _nova._execute_tool_sync
_gemini_generate_with_delay = _nova._gemini_generate_with_delay
add_memory_fact = _nova.add_memory_fact

def _is_rate_limited(*args, **kwargs):
    fn = getattr(_nova, "_is_rate_limited", None)
    if fn is None:
        return False
    return fn(*args, **kwargs)


def _record_rate_limit(*args, **kwargs):
    fn = getattr(_nova, "_record_rate_limit", None)
    if fn is None:
        return None
    return fn(*args, **kwargs)


def _reset_rate_limit(*args, **kwargs):
    fn = getattr(_nova, "_reset_rate_limit", None)
    if fn is None:
        return None
    return fn(*args, **kwargs)

def build_memory_context(*args, **kwargs):
    fn = getattr(_nova, "build_memory_context", None)
    if fn is None:
        return None
    return fn(*args, **kwargs)

def extract_memory_updates(*args, **kwargs):
    fn = getattr(_nova, "extract_memory_updates", None)
    if fn is None:
        return None
    return fn(*args, **kwargs)
is_online = _nova.is_online

class NOVALive:
    def __init__(self, meta: dict) -> None:
        self.meta     = meta
        self.session  = None
        self.audio_in_queue: Optional[asyncio.Queue] = None
        self.out_queue:      Optional[asyncio.Queue] = None
        self._loop           = None
        self._is_speaking    = False
        self._speaking_lock  = threading.Lock()
        self._retry_count    = 0
        self._turn_done      = False
        self._has_greeted    = False
        self._last_speak_end = 0.0
        self._play_q         = None
        self._gate           = None
        self._barge_in_pending = False
        # Diagnostic counters — stage 0: did __init__ even reach here?
        self._diag_mic_chunks_sent   = 0
        self._diag_mic_chunks_muted  = 0
        self._diag_ws_bytes_out      = 0
        self._diag_ws_bytes_in       = 0
        self._diag_play_chunks       = 0
        self._diag_play_errors       = 0
        self._diag_callback_status_flags = 0
        _diag("INIT", f"NOVALive constructed. HAS_GEMINI={HAS_GEMINI} "
                       f"GEMINI_API_KEY_present={bool(GEMINI_API_KEY)} LIVE_MODEL={LIVE_MODEL}")
        try:
            devs = sd.query_devices()
            default_in, default_out = sd.default.device
            _diag("AUDIO_DEVICES", f"default_in_idx={default_in} default_out_idx={default_out} "
                                    f"in_name={devs[default_in]['name'] if default_in is not None and default_in >= 0 else 'NONE'} "
                                    f"out_name={devs[default_out]['name'] if default_out is not None and default_out >= 0 else 'NONE'}")
        except Exception as e:
            _diag("AUDIO_DEVICES", f"⚠️ Could not query audio devices: {e!r} — mic/speaker may not exist or PortAudio not installed")

    def _build_config(self) -> Any:
        if not HAS_GEMINI or gtypes is None:
            raise RuntimeError("Gemini API not available. Set GOOGLE_API_KEY environment variable.")
        mem_ctx    = build_memory_context(self.meta)
        time_str   = datetime.now().strftime("%A, %B %d, %Y — %I:%M %p")
        sys_prompt = NOVA_SYSTEM_PROMPT.replace(
            datetime.now().strftime("%A, %B %d, %Y — %I:%M %p"), time_str
        )
        if mem_ctx:
            sys_prompt += f"\n\n[BACKGROUND MEMORY — only use if user asks about these topics]\n{mem_ctx}"
        _diag("CONFIG", f"Built LiveConnectConfig. sys_prompt_len={len(sys_prompt)} "
                         f"mem_ctx_len={len(mem_ctx or '')} voice={NOVA_VOICE} "
                         f"tool_count={len(TOOL_DECLARATIONS)}")
        return gtypes.LiveConnectConfig(
            response_modalities=[gtypes.Modality.AUDIO],
            output_audio_transcription=gtypes.AudioTranscriptionConfig(),
            input_audio_transcription=gtypes.AudioTranscriptionConfig(),
            system_instruction=sys_prompt,
            tools=[{"function_declarations": TOOL_DECLARATIONS}],
            session_resumption=gtypes.SessionResumptionConfig(),
            speech_config=gtypes.SpeechConfig(
                voice_config=gtypes.VoiceConfig(
                    prebuilt_voice_config=gtypes.PrebuiltVoiceConfig(voice_name=NOVA_VOICE)
                )
            ),
        )

    def _set_speaking(self, value: bool) -> None:
        with self._speaking_lock:
            self._is_speaking = value
            if not value:
                self._last_speak_end = time.time()
        state = "🔊 SPEAKING" if value else "🎙️  LISTENING"
        print(f"\r{state}  ", end="", flush=True)

    def _barge_in(self) -> None:
        """User started speaking over NOVA — stop playback NOW and yield the mic."""
        if not self._is_speaking:
            return
        self._barge_in_pending = True
        _diag("BARGE", "⚠️ user speech detected during NOVA output — interrupting playback")
        self._set_speaking(False)
        # 1) Empty the asyncio queue feeding playback (Gemini→speaker)
        if self.audio_in_queue is not None:
            while True:
                try:
                    self.audio_in_queue.get_nowait()
                except (asyncio.QueueEmpty, Exception):
                    break
        # 2) Empty the playback worker's own queue (already-decoded int16 chunks)
        _pq = self._play_q
        if _pq is not None:
            try:
                with _pq.mutex:
                    _pq.queue.clear()
            except Exception:
                pass
        _diag("BARGE", f"playback queues drained — audio_in_queue_size_now={self.audio_in_queue.qsize() if self.audio_in_queue else -1}")
        self._barge_in_pending = False

    def speak(self, text: str) -> None:
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"role": "user", "parts": [{"text": text}]}, turn_complete=True
            ),
            self._loop,
        )

    async def _send_greeting(self) -> None:
        """Fires once per process — greets proactively instead of waiting in silence."""
        if self.session is None:
            return
        await asyncio.sleep(0.4)  # let _receive_audio start consuming first
        user_name = (self.meta.get("user_name") or "").strip()
        name_part = user_name if user_name else "there"
        prompt = (
            f"The user just opened this session. Greet {name_part} right now, out loud, "
            "in 1-2 short spoken sentences — warm, a little informal, Nigerian-aware where "
            "it fits naturally. Vary your phrasing each time, don't reuse a stock line. "
            "End by asking what's on their mind or what you can help with today. "
            "Do not wait for them to speak first."
        )
        try:
            await self.session.send_client_content(
                turns={"role": "user", "parts": [{"text": prompt}]}, turn_complete=True
            )
        except Exception as e:
            print(f"[NOVA] ⚠️ Greeting failed: {e}")

    async def _execute_tool(self, fc: Any) -> Any:
        name   = fc.name
        args   = dict(fc.args or {})
        print(f"\n🔧 Tool: {name}({json.dumps(args, ensure_ascii=False)[:100]})")
        loop   = asyncio.get_event_loop()
        result = "Done."
        try:
            result = await loop.run_in_executor(
                _executor, lambda: _execute_tool_sync(name, args, self.meta)
            )
            if name == "remember_fact" and args.get("fact"):
                add_memory_fact(args["fact"], self.meta)
            if name in (
                "open_app", "computer_control", "computer_settings",
                "file_controller", "browser_control", "file_processor",
                "autostart", "planner", "self_editor",
            ):
                self.speak(f"Done. {str(result)[:120]}")
        except Exception as e:
            log.exception(f"Tool failed: {name}")
            result = f"Tool '{name}' encountered an error: {str(e)[:100]}"
            self.speak(f"Sorry, {name} failed. {str(e)[:80]}")
        print(f"📤 Result: {str(result)[:120]}")
        response = result if isinstance(result, dict) else {"output": str(result)}
        return gtypes.FunctionResponse(id=fc.id, name=name, response=response)

    async def _send_realtime(self) -> None:
        if self.session is None or self.out_queue is None:
            _diag("SEND", "⚠️ ABORT: session or out_queue is None at task start")
            return
        _diag("SEND", "task started, entering send loop")
        consecutive_failures = 0
        sent_count = 0
        while True:
            msg = await self.out_queue.get()
            if self.session is None:
                _diag("SEND", "session went None mid-loop — dropping message, NOT exiting task")
                continue
            try:
                await self.session.send_realtime_input(audio=msg)
                self._diag_ws_bytes_out += len(msg.get("data", b""))
                sent_count += 1
                if consecutive_failures > 0:
                    _diag("SEND", f"recovered after {consecutive_failures} consecutive failures")
                consecutive_failures = 0
                if sent_count == 1:
                    _diag("SEND", "first chunk sent to Gemini Live successfully")
                elif sent_count % 100 == 0:
                    _diag("SEND", f"{sent_count} chunks sent OK, queue_depth={self.out_queue.qsize()}")
            except Exception as e:
                consecutive_failures += 1
                print(f"[NOVA] ⚠️ send_realtime_input failed: {e}")
                _diag("SEND", f"⚠️ send FAILED ({consecutive_failures} in a row): {type(e).__name__}: {e} "
                               f"— exception is being swallowed here, task keeps looping on a dead session")
                if consecutive_failures >= 5:
                    _diag("SEND", "🔴 5+ consecutive send failures — forcing session teardown so "
                                   "receive_audio fails fast and run()'s TaskGroup reconnects, instead "
                                   "of looping silently on a dead session.")
                    raise RuntimeError(
                        f"send_realtime_input failed {consecutive_failures}x consecutively — session dead"
                    )

    async def _listen_audio(self) -> None:
        print("🎙️  Mic open")
        _diag("MIC", "task started")
        loop      = asyncio.get_event_loop()
        out_queue = self.out_queue
        if out_queue is None:
            _diag("MIC", "🔴 ABORT: out_queue is None — cannot start mic capture")
            raise RuntimeError("out_queue not initialized")

        _cb_count = {"n": 0, "dropped": 0}
        _rms_window = {"sum": 0.0, "n": 0, "max": 0.0}

        def _request_barge_in() -> None:
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._barge_in)

        # Listening policy lives in nova_voice so the terminal and the desktop
        # cannot drift apart. The thresholds below are the ones this file used
        # to define inline; they now have one home.
        self._gate = nova_voice.VoiceGate(
            chunk_samples=CHUNK_SIZE, on_barge_in=_request_barge_in,
        )

        def _callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            _cb_count["n"] += 1
            if status:
                # PortAudio flags input overflow/underflow — previously silently ignored.
                self._diag_callback_status_flags += 1
                _diag("MIC", f"⚠️ PortAudio status flag on callback #{_cb_count['n']}: {status} "
                              f"(input_overflow means chunks were dropped by the OS before we ever saw them)")

            # nova_voice.VoiceGate decides what actually goes on the wire.
            payload = self._gate.process(np.asarray(indata).reshape(-1))
            data = {"data": payload, "mime_type": "audio/pcm;rate=16000"}
            speaking = self._gate.speaking

            # Keep the signal diagnostics that made mic problems debuggable:
            # they report on the audio actually being transmitted.
            if not speaking:
                self._diag_mic_chunks_sent += 1
                rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
                _rms_window["sum"] += rms
                _rms_window["n"] += 1
                _rms_window["max"] = max(_rms_window["max"], rms)
            else:
                self._diag_mic_chunks_muted += 1

            if _cb_count["n"] == 1:
                _diag("MIC", f"first callback fired — frames={frames}, dtype={indata.dtype}, "
                              f"shape={getattr(indata, 'shape', '?')}, speaking={speaking}")
            elif _cb_count["n"] % 500 == 0:
                avg_rms = (_rms_window["sum"] / _rms_window["n"]) if _rms_window["n"] else 0.0
                _diag("MIC", f"{_cb_count['n']} callbacks total | sent={self._diag_mic_chunks_sent} "
                              f"muted={self._diag_mic_chunks_muted} dropped_queue_full={_cb_count['dropped']} "
                              f"status_flags={self._diag_callback_status_flags}")
                _diag("MIC", f"signal check (int16 raw amplitude, silence≈0, normal speech≈500-5000+): "
                              f"avg_rms={avg_rms:.1f} peak_rms={_rms_window['max']:.1f} "
                              f"over last {_rms_window['n']} real chunks — "
                              f"{'⚠️ LOOKS SILENT/NEAR-ZERO, mic may not be capturing you' if avg_rms < 50 else 'signal present'}")
                _rms_window["sum"] = 0.0
                _rms_window["n"] = 0
                _rms_window["max"] = 0.0

            def _safe_put() -> None:
                try:
                    out_queue.put_nowait(data)
                except asyncio.QueueFull:
                    _cb_count["dropped"] += 1
                    if _cb_count["dropped"] in (1, 10, 50) or _cb_count["dropped"] % 200 == 0:
                        _diag("MIC", f"⚠️ out_queue FULL — dropped chunk #{_cb_count['dropped']} "
                                      f"(consumer/_send_realtime is falling behind or stalled)")

            loop.call_soon_threadsafe(_safe_put)

        try:
            with sd.InputStream(
                samplerate=SEND_SAMPLE_RATE, channels=CHANNELS, dtype="int16",
                blocksize=CHUNK_SIZE, callback=_callback,
            ):
                print("🎙️  Mic stream active — echo suppressed during playback")
                _diag("MIC", f"InputStream opened OK — samplerate={SEND_SAMPLE_RATE} "
                              f"channels={CHANNELS} blocksize={CHUNK_SIZE}")
                while True:
                    await asyncio.sleep(0.1)
        except Exception as e:
            print(f"❌ Mic error: {e}")
            _diag("MIC", f"🔴 InputStream FAILED to open or crashed: {type(e).__name__}: {e} "
                          f"— check OS mic permissions and that a default input device exists")
            raise

    async def _receive_audio(self) -> None:
        print("👂 Receiving...")
        _diag("RECV", "task started, calling session.receive()")
        _connect_t = time.time()
        _first_response = True
        _resp_count = 0
        out_buf: List[str] = []
        in_buf:  List[str] = []
        try:
            while True:
                if self.session is None:
                    _diag("RECV", "session is None — exiting receive loop")
                    break
                async for response in self.session.receive():
                    _resp_count += 1
                    if _first_response:
                        _diag("RECV", f"🟢 first response arrived {time.time() - _connect_t:.2f}s after "
                                       f"connect — websocket + Gemini session are alive and talking")
                        _first_response = False
                    if response is None:
                        _diag("RECV", "got None response object — skipping")
                        continue
                    if response.server_content is not None and self.audio_in_queue is not None:
                        _sc = response.server_content
                        _mt = getattr(_sc, "model_turn", None)
                        _parts = getattr(_mt, "parts", None) or []
                        for _part in _parts:
                            _inline = getattr(_part, "inline_data", None)
                            if _inline is not None and getattr(_inline, "data", None):
                                self._diag_ws_bytes_in += len(_inline.data)
                                self._set_speaking(True)
                                self.audio_in_queue.put_nowait(_inline.data)
                    if response.server_content:
                        sc = response.server_content
                        if sc and sc.output_transcription and sc.output_transcription.text:
                            self._set_speaking(True)
                            txt = sc.output_transcription.text.strip()
                            if txt:
                                out_buf.append(txt)
                        if sc and sc.input_transcription and sc.input_transcription.text:
                            txt = sc.input_transcription.text.strip()
                            if txt:
                                in_buf.append(txt)
                        if sc and getattr(sc, "interrupted", False):
                            # Server cut off its own generation because the user's
                            # audio reached it (barge-in) — stop playback promptly.
                            if not self._is_speaking:
                                continue
                            self._barge_in()
                            continue
                        if sc and sc.turn_complete:
                            self._turn_done = True
                            self._set_speaking(False)
                            full_in = " ".join(in_buf).strip()
                            if full_in:
                                print(f"\n👤 You: {full_in}")
                            in_buf = []
                            full_out = " ".join(out_buf).strip()
                            if full_out:
                                print(f"🤖 NOVA: {full_out}")
                            out_buf = []
                            if _nova._nova_memory and (full_in or full_out):
                                if full_in:
                                    _nova._nova_memory.log_turn("user", full_in)
                                if full_out:
                                    _nova._nova_memory.log_turn("assistant", full_out)
                            if full_in and len(full_in) > 5:
                                _nova._mem_extract_turn_counter += 1
                                if _nova._mem_extract_turn_counter % _MEM_EXTRACT_EVERY_N == 0:
                                    threading.Thread(
                                        target=lambda u=full_in, a=full_out: extract_memory_updates(u, a, self.meta),
                                        daemon=True,
                                    ).start()
                            # [living memory] feed finished turn
                            _living_turn = getattr(_nova, "_living_turn", None)
                            if _living_turn and (full_in or full_out):
                                try:
                                    _living_turn(full_in, full_out)
                                except Exception:
                                    pass
                    if response.tool_call and response.tool_call.function_calls:
                        for fc in response.tool_call.function_calls:
                            _diag("RECV", f"tool_call received: {fc.name}({dict(fc.args or {})})")
                        responses = []
                        for fc in response.tool_call.function_calls:
                            fr = await self._execute_tool(fc)
                            responses.append(fr)
                        if self.session is not None:
                            try:
                                await self.session.send_tool_response(function_responses=responses)
                            except Exception as e:
                                print(f"[NOVA] ⚠️ send_tool_response failed: {e}")
                                _diag("RECV", f"⚠️ send_tool_response FAILED: {type(e).__name__}: {e} "
                                              f"— tool ran but Gemini never got the result, model will likely stall")
                                # Don't re-raise — keep the loop alive
                if _resp_count and _resp_count % 200 == 0:
                    _diag("RECV", f"{_resp_count} responses processed so far")
        except Exception as e:
            _diag("RECV", f"🔴 receive loop CRASHED: {type(e).__name__}: {e} "
                          f"— this will propagate to TaskGroup and cancel ALL sibling tasks (mic/send/play)")
            traceback.print_exc()
            self._set_speaking(False)
            raise

    async def _play_audio(self) -> None:
        if self.audio_in_queue is None:
            _diag("PLAY", "🔴 ABORT: audio_in_queue is None")
            raise RuntimeError("audio_in_queue not initialized")
        _diag("PLAY", "task started")
        import queue as _q
        _play_q: _q.Queue = _q.Queue(maxsize=200)
        self._play_q = _play_q
        _write_errors = {"n": 0}

        def _play_worker() -> None:
            # Dedicated thread — no asyncio overhead per chunk
            try:
                stream = sd.RawOutputStream(
                    samplerate=RECEIVE_SAMPLE_RATE, channels=CHANNELS,
                    dtype="int16",
                    blocksize=CHUNK_SIZE * 4,   # larger block = smoother playback
                    latency="low",
                )
                stream.start()
            except Exception as e:
                _diag("PLAY", f"🔴 RawOutputStream FAILED to open: {type(e).__name__}: {e} "
                              f"— check OS speaker permissions / default output device exists. "
                              f"Playback thread is dead; NOVA will receive audio but never play it.")
                return
            _diag("PLAY", f"output stream open OK — samplerate={RECEIVE_SAMPLE_RATE} blocksize={CHUNK_SIZE*4}")
            try:
                while True:
                    chunk = _play_q.get()
                    if chunk is None:
                        break
                    try:
                        # Tell the shared voice policy what the room is about
                        # to hear. Without this the echo canceller in
                        # nova_voice has no reference to cancel against, and
                        # the terminal falls back to a plain loudness test —
                        # the exact behaviour that made the desktop interrupt
                        # itself. Both surfaces run one policy; both have to
                        # feed it.
                        gate = self._gate
                        if gate is not None:
                            gate.reference(chunk, RECEIVE_SAMPLE_RATE)
                        stream.write(chunk)
                        self._diag_play_chunks += 1
                        if self._diag_play_chunks == 1:
                            _diag("PLAY", "first audio chunk written to speaker successfully")
                    except Exception as e:
                        # PREVIOUSLY: silently swallowed with bare `except Exception: pass`.
                        # That is a real silent-failure candidate for "no sound" — instrumented, not fixed yet.
                        _write_errors["n"] += 1
                        self._diag_play_errors += 1
                        if _write_errors["n"] in (1, 5, 20) or _write_errors["n"] % 100 == 0:
                            _diag("PLAY", f"⚠️ stream.write FAILED ({_write_errors['n']} times so far): "
                                          f"{type(e).__name__}: {e} — this was previously silently discarded")
            finally:
                stream.stop()
                stream.close()

        _pt = threading.Thread(target=_play_worker, daemon=True, name="AudioPlay")
        _pt.start()
        print("🔊 Speaker ready")
        _play_drops = {"n": 0}
        _first_recv_chunk = True
        try:
            while True:
                chunk = await self.audio_in_queue.get()
                if _first_recv_chunk:
                    _diag("PLAY", "first chunk pulled from audio_in_queue (Gemini→speaker handoff working)")
                    _first_recv_chunk = False
                self._set_speaking(True)
                try:
                    _play_q.put_nowait(chunk)
                except _q.Full:
                    _play_drops["n"] += 1
                    if _play_drops["n"] in (1, 10) or _play_drops["n"] % 100 == 0:
                        _diag("PLAY", f"⚠️ playback queue FULL — dropped chunk #{_play_drops['n']} "
                                      f"(speaker thread can't keep up, or is dead/blocked)")
                    pass   # drop oldest would be better but put_nowait is safe
                if self.audio_in_queue.empty() and self._turn_done:
                    self._set_speaking(False)
                    self._turn_done = False
        except Exception as e:
            print(f"❌ Playback error: {e}")
            _diag("PLAY", f"🔴 playback consumer loop CRASHED: {type(e).__name__}: {e}")
            raise
        finally:
            _play_q.put(None)   # signal worker to stop
            self._set_speaking(False)

    def _finish_turn(self, audio_in_queue) -> None:
        """Called when NOVA finishes speaking — reset state and signal Gemini."""
        self._set_speaking(False)
        
        # Signal turn completion to Gemini
        if self.session is not None and self._loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(
                    self.session.send_client_content(turns=[], turn_complete=True),
                    self._loop
                )
                print("[NOVA] 📤 Turn complete signal sent")
            except Exception as e:
                print(f"[NOVA] ⚠️ Turn signal failed: {e}")
        
        # Drain any echo/stale audio
        drained = 0
        while not audio_in_queue.empty():
            try:
                audio_in_queue.get_nowait()
                drained += 1
            except asyncio.QueueEmpty:
                break
        if drained:
            print(f"[NOVA] Drained {drained} stale chunks")
        
        print("\n🎙️  LISTENING — speak now...")

    async def run(self) -> str:
        client = genai.Client(api_key=GEMINI_API_KEY, http_options={"api_version": "v1beta"})
        self._retry_count = 0  # Reset once at start
        _diag("RUN", f"NOVALive.run() entered. GEMINI_API_KEY_present={bool(GEMINI_API_KEY)}")
        while True:
            if not is_online():
                print("[NOVA] 📴 No internet connection. Switching to offline mode...")
                _diag("RUN", "is_online() returned False — never attempted Gemini connect")
                return "offline"
            try:
                print("\n[NOVA] 🔌 Connecting to Gemini Live...")
                _diag("WS", f"attempting client.aio.live.connect(model={LIVE_MODEL!r})")
                _connect_start = time.time()
                config = self._build_config()
                async with client.aio.live.connect(model=LIVE_MODEL, config=config) as session, asyncio.TaskGroup() as tg:
                    self.session        = session
                    self._loop          = asyncio.get_event_loop()
                    self.audio_in_queue = asyncio.Queue()
                    self.out_queue      = asyncio.Queue(maxsize=20)
                    _diag("WS", f"🟢 session established in {time.time() - _connect_start:.2f}s")
                    # NO retry reset here!
                    print("[NOVA] ✅ Gemini Live connected.")
                    print("[NOVA] 🎙️  Speak to NOVA — say 'goodbye NOVA' to exit.\n")

                    def _on_task_done(name: str):
                        def _cb(t: "asyncio.Task[Any]") -> None:
                            if t.cancelled():
                                _diag("TASK", f"'{name}' was cancelled (normal during shutdown/reconnect, "
                                              f"or a SIBLING task crashed first and TaskGroup is cancelling everyone)")
                                return
                            exc = t.exception()
                            if exc is not None:
                                _diag("TASK", f"🔴 '{name}' raised {type(exc).__name__}: {exc} "
                                              f"— THIS IS LIKELY THE FIRST FAILING COMPONENT if it's the "
                                              f"earliest 'TASK' error line in this run")
                        return _cb

                    for coro, name in (
                        (self._send_realtime(),  "send_realtime"),
                        (self._listen_audio(),   "listen_audio"),
                        (self._receive_audio(),  "receive_audio"),
                        (self._play_audio(),     "play_audio"),
                        (self._send_greeting(),  "send_greeting"),
                    ):
                        t = tg.create_task(coro, name=name)
                        t.add_done_callback(_on_task_done(name))
                        _diag("TASK", f"created '{name}'")
            except KeyboardInterrupt:
                print("\n[NOVA] 🔴 Shutting down.")
                return "exit"
            except Exception as e:
                import traceback
                sub_errors = getattr(e, "exceptions", None)
                _diag("RUN", f"🔴 connect/TaskGroup raised {type(e).__name__}: {e} "
                              f"(has_sub_exceptions={sub_errors is not None})")
                if sub_errors is not None:
                    print(f"[NOVA] ⚠️  Connection error: {e}")
                    for i, sub in enumerate(sub_errors):
                        print(f"[NOVA] 📋 Sub-error: {sub}")
                        _diag("RUN", f"sub-exception[{i}] = {type(sub).__name__}: {sub}")
                        traceback.print_exception(type(sub), sub, sub.__traceback__)
                else:
                    print(f"[NOVA] ⚠️  Connection error: {e}")
                    traceback.print_exception(type(e), e, e.__traceback__)
                self._retry_count += 1
                self._set_speaking(False)
                if self.session is not None:
                    try:
                        await self.session.close()
                    except Exception:
                        pass
                self.audio_in_queue = None
                self.out_queue = None
                self.session = None
                self._loop = None
                if self._retry_count >= MAX_GEMINI_RETRIES:
                    print(f"[NOVA] ⚠️  Gemini unreachable after {self._retry_count} retries. Falling back to offline...")
                    _diag("RUN", f"exhausted {self._retry_count} retries — returning 'offline'")
                    return "offline"
                wait_s = min(3 * self._retry_count, 12)
                print(f"[NOVA] 🔄 Reconnecting in {wait_s}s... (attempt {self._retry_count}/{MAX_GEMINI_RETRIES})")
                _diag("RUN", f"sleeping {wait_s}s before reconnect attempt {self._retry_count + 1}")
                await asyncio.sleep(wait_s)
        return "offline"


# ══════════════════════════════════════════════════════════════════════════════
#  OFFLINE BRAIN — OVERHAULED v4.0
#  - Fixed tool calling for Ollama
#  - Fixed agent hijacking
#  - Added offline Wikipedia (ZIM)
#  - Added offline maps
#  - Unified mic loop with online mode
#  - Proper state machine
# ══════════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════
#  MISSING CORE FUNCTIONS (restored from bak)
# ══════════════════════════════════════════════════════════════════════════════

def _call_gemini_chat(
    messages: List[Dict[str, Any]],
    use_tools: bool = True,
) -> Optional[Dict[str, Any]]:
    """Gemini REST chat — converts OpenAI-style messages to Gemini format.
    Returns {"text": str, "tool_calls": list} or None on failure."""
    if not (HAS_GEMINI and GEMINI_API_KEY):
        return None
    if _is_rate_limited():
        return None
    try:
        client   = genai.Client(
            api_key=GEMINI_API_KEY,
            http_options=gtypes.HttpOptions(timeout=15000),
        )
        contents: List[Any] = []
        system_txt = ""

        for msg in messages:
            role    = msg.get("role", "user")
            content = msg.get("content") or ""
            if role == "system":
                system_txt = content
                continue
            gemini_role = "model" if role == "assistant" else "user"
            # gtypes.Part.from_text may not accept positional args in some genai versions;
            # construct Part using keyword 'text' for compatibility.
            contents.append(
                gtypes.Content(role=gemini_role, parts=[gtypes.Part(text=content)])
            )

        cfg_kwargs: Dict[str, Any] = {}
        if system_txt:
            cfg_kwargs["system_instruction"] = system_txt
        if use_tools and TOOL_DECLARATIONS:
            cfg_kwargs["tools"] = [{"function_declarations": TOOL_DECLARATIONS}]

        response = _gemini_generate_with_delay(
            client,
            model=VISION_MODEL,
            contents=contents,
            config=gtypes.GenerateContentConfig(**cfg_kwargs) if cfg_kwargs else None,
        )
        _reset_rate_limit()

        text: str = (response.text or "").strip()
        tool_calls: List[Dict[str, Any]] = []

        for cand in (response.candidates or []):
            for part in (getattr(cand.content, "parts", None) or []):
                fc = getattr(part, "function_call", None)
                if fc:
                    tool_calls.append({
                        "id":   str(int(time.time())),
                        "name": fc.name,
                        "args": dict(fc.args or {}),
                    })

        return {"text": text, "tool_calls": tool_calls}

    except Exception as e:
        err = str(e)
        if "429" in err or "RESOURCE_EXHAUSTED" in err:
            _record_rate_limit()
        else:
            log.error(f"Gemini chat error: {e}")
        return None


def _trim_history() -> None:
    """Trim _nova.conversation_history to the last MAX_HISTORY_TURNS pairs."""
    max_entries = MAX_HISTORY_TURNS * 2
    if len(_nova.conversation_history) > max_entries:
        _nova.conversation_history = _nova.conversation_history[-max_entries:]


def get_text_input() -> str:
    """Read a line of text from the keyboard."""
    try:
        return input("You: ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def _load_whisper_async() -> None:
    """Load faster-whisper STT model in a background thread."""
    if not HAS_FASTER_WHISPER:
        log.warning("faster-whisper not installed — STT unavailable.")
        _stt_loaded.set()
        return
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log.error("faster-whisper failed to import at load time — STT unavailable.")
        _stt_loaded.set()
        return
    try:
        log.info(f"Loading Whisper model ({WHISPER_MODEL_SIZE})...")
        with _stt_model_lock:
            _nova._stt_model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
        log.info("✅ Whisper model loaded.")
    except Exception as e:
        log.error(f"Whisper load failed: {e}")
    finally:
        _stt_loaded.set()


def calibrate_ambient_noise(duration: float = 2.5, fs: int = 16000) -> float:
    """Record a short silence sample and set _nova.AMBIENT_THRESHOLD."""
    try:
        print(f"🎙️  Calibrating ambient noise ({duration}s) — stay quiet...")
        recording = sd.rec(int(duration * fs), samplerate=fs, channels=1, dtype="float32")
        sd.wait()
        rms = float(np.sqrt(np.mean(recording ** 2)))
        _nova.AMBIENT_THRESHOLD = max(rms * 2.5, DEFAULT_THRESHOLD)
        print(f"✅ Threshold set: {_nova.AMBIENT_THRESHOLD:.4f}")
        return _nova.AMBIENT_THRESHOLD
    except Exception as e:
        log.warning(f"Calibration failed: {e} — using default threshold.")
        _nova.AMBIENT_THRESHOLD = DEFAULT_THRESHOLD
        return _nova.AMBIENT_THRESHOLD


# ══════════════════════════════════════════════════════════════════════════════
#  OFFLINE BRAIN — OVERHAULED v4.0
# ─── CONFIGURATION ───────────────────────────────────────────────────────────

OFFLINE_MODELS = ["tinyllama", "llama3.2", "phi3", "mistral"]
OFFLINE_TIMEOUTS = {"tinyllama": 15, "llama3.2": 30, "phi3": 30, "mistral": 45}

# ZIM/Wikipedia paths
def _maps_data_path() -> str:
    """Offline map data location — see _zim_data_path for why this is derived."""
    if getattr(sys, "frozen", False):
        base = os.getenv("APPDATA")
        root = Path(base) / "NOVA" if base else Path.home() / ".nova"
        return str(root / "data" / "maps")
    override = os.getenv("NOVA_MAPS_DIR", "").strip()
    if override:
        return override
    return str(Path(__file__).resolve().parent / "data" / "maps")


OFFLINE_MAPS_PATH = _maps_data_path()

# Tool declarations for Ollama (must match Ollama's expected format)
OLLAMA_TOOL_FORMAT = {
    "type": "function",
    "function": {
        "name": "",
        "description": "",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
}

# ─── OFFLINE STATE MACHINE ─────────────────────────────────────────────────────