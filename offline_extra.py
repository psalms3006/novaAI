"""offline_extra.py — offline fallback stack (Ollama/Whisper/pyttsx3/Piper/Wiki/Maps), extracted from nova.py (Phase 2)."""
from __future__ import annotations
import html as _html
import json, os, queue, re, subprocess, sys, tempfile, threading, time
from pathlib import Path
from typing import Any, Dict, List, Optional
import numpy as np
import requests
import sounddevice as sd
from scipy.io import wavfile as wav_write

import nova_state
import nova as _nova
log = _nova.log


def _nova_get(name, default=None):
    try:
        return getattr(_nova, name, default)
    except Exception:
        return default


HAS_GEMINI = _nova_get('HAS_GEMINI', False)
GEMINI_API_KEY = _nova_get('GEMINI_API_KEY')
TOOL_DECLARATIONS = _nova_get('TOOL_DECLARATIONS', [])
NOVA_OFFLINE_PROMPT = _nova_get('NOVA_OFFLINE_PROMPT', '')
FORCE_OFFLINE = _nova_get('FORCE_OFFLINE', False)
TEXT_MODE = _nova_get('TEXT_MODE', False)
OFFLINE_MODELS = _nova_get('OFFLINE_MODELS', [])
OFFLINE_TIMEOUTS = _nova_get('OFFLINE_TIMEOUTS', {})
ZIM_DATA_PATH = _nova_get('ZIM_DATA_PATH')
OFFLINE_MAPS_PATH = _nova_get('OFFLINE_MAPS_PATH')
DEFAULT_THRESHOLD = _nova_get('DEFAULT_THRESHOLD', 0.04)
TTS_RATE = _nova_get('TTS_RATE', 165)
TTS_VOLUME = _nova_get('TTS_VOLUME', 0.95)
PIPER_MODEL = _nova_get('PIPER_MODEL')
PIPER_RATE = _nova_get('PIPER_RATE', 22050)
_MEM_EXTRACT_EVERY_N = _nova_get('_MEM_EXTRACT_EVERY_N', 5)
_stt_loaded = _nova_get('_stt_loaded')
_stt_model_lock = _nova_get('_stt_model_lock')


def _get_pyttsx3():
    try:
        return getattr(_nova, 'pyttsx3', None)
    except Exception:
        return None


pyttsx3 = _get_pyttsx3()

def _nova_call(name, *args, **kwargs):
    fn = _nova_get(name)
    if callable(fn):
        return fn(*args, **kwargs)
    return None

is_online = _nova_get('is_online')
is_ollama_running = _nova_get('is_ollama_running')
check_network_recovery = _nova_get('check_network_recovery')
offline_greeting = _nova_get('offline_greeting')
add_memory_fact = _nova_get('add_memory_fact')
build_memory_context = _nova_get('build_memory_context')
get_all_memory_text = _nova_get('get_all_memory_text')
extract_memory_updates = _nova_get('extract_memory_updates')
_execute_tool_sync = _nova_get('_execute_tool_sync')
agent_process = _nova_get('agent_process')
_call_gemini_chat = _nova_get('_call_gemini_chat')
_trim_history = _nova_get('_trim_history')


def _keyboard_input() -> str:
    """Robust keyboard input for --text mode (never touches the mic)."""
    try:
        text = input("You: ").strip()
        return text
    except (EOFError, KeyboardInterrupt):
        return ""


get_text_input = _nova_get('get_text_input') or _keyboard_input
_load_whisper_async = _nova_get('_load_whisper_async')
calibrate_ambient_noise = _nova_get('calibrate_ambient_noise')

class OfflineState:
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    PROCESSING = "PROCESSING"
    SPEAKING = "SPEAKING"
    THINKING = "THINKING"

_offline_state = OfflineState.IDLE
_offline_state_lock = threading.Lock()
_tts_queue = queue.Queue()
_tts_done_event = threading.Event()

def set_offline_state(state: str):
    global _offline_state
    with _offline_state_lock:
        _offline_state = state
    print(f"   [{state}]")

def get_offline_state() -> str:
    with _offline_state_lock:
        return _offline_state

# ─── OFFLINE TTS ENGINE (NON-BLOCKING) ────────────────────────────────────────

def _offline_tts_worker():
    """Background thread for offline TTS. Never blocks the main loop."""
    while True:
        item = _tts_queue.get()
        if item is None:  # Shutdown signal
            break
        text, done_event = item
        set_offline_state(OfflineState.SPEAKING)
        _speak_offline_impl(text)
        set_offline_state(OfflineState.LISTENING)
        done_event.set()

# Start TTS worker
_tts_thread = threading.Thread(target=_offline_tts_worker, daemon=True)
_tts_thread.start()

# ─── OFFLINE BARGE-IN ──────────────────────────────────────────────────────
#
# Reuses nova_voice.VoiceGate/EchoCanceller -- the same mechanism the (off by
# default; see simple_voice_default()) full-duplex cloud path uses -- rather
# than a second, offline-specific echo/VAD heuristic. terminal_voice.py exists
# because two implementations of the same thing drift; a second barge-in
# detector would be that mistake again. Forced to full-duplex here regardless
# of NOVA_VOICE_FULL_DUPLEX: offline has no interruption button, so the
# mute-only "simple" policy would mean no barge-in is possible at all -- the
# "wait until she finishes" behaviour real barge-in exists to avoid.
#
# First real-hardware run (2026-09-22) produced continuous false "speech
# detected", TTS chopped into fragments, and audio not reaching the user's
# headset at all -- diagnosed (from code, not from that run's log: the
# per-chunk diagnostics below did not exist yet, and print()-only messages
# never reached nova.log) to two causes:
#
# 1. Device selection. Neither the mic feed nor TTS playback named a device,
#    so both used whatever Windows called "default" -- which is not
#    necessarily the headset. desk/live_session.py already solves this
#    (mic_device/speaker_device settings, WASAPI auto_convert for the sample
#    rate mismatch a shared-mode device enforces) for the cloud path; offline
#    ignored all of it and could easily have ended up playing through one
#    device while "listening" on another with tight acoustic coupling to it
#    -- which would explain both symptoms as one cause, not two.
# 2. Mic timing. The mic ran on a blocking stream.read() loop on a plain
#    Python thread, subject to GIL/thread-wake jitter, instead of a
#    PortAudio-scheduled callback. VoiceGate's echo alignment assumes frames
#    arrive on a steady cadence; desk/live_session.py's own microphone has
#    always used a callback for exactly this reason.
#
# Both are fixed below by reusing desk.live_session's own device resolution
# and callback pattern instead of a second, looser implementation of the
# same thing. This is NOT verified against real hardware -- see the
# diagnostics added alongside it.

#: Mic rate barge-in listens at. Matches listen_offline()'s own `fs`.
_BARGE_IN_MIC_RATE = 16000

_offline_gate_lock = threading.Lock()
_offline_gate = None
_offline_barge_event = threading.Event()
_offline_devices_logged = threading.Event()


def _on_offline_barge_in() -> None:
    _offline_barge_event.set()


def _get_offline_gate():
    global _offline_gate
    with _offline_gate_lock:
        if _offline_gate is None:
            import nova_voice
            _offline_gate = nova_voice.VoiceGate(
                chunk_samples=1024, on_barge_in=_on_offline_barge_in,
                simple=False,
            )
        return _offline_gate


def _offline_input_device():
    """The same configured mic (mic_device setting / NOVA_MIC_DEVICE) the
    cloud path resolves to, or None for the system default. Resolved fresh
    each call, matching desk.live_session's own reasoning: a cached index
    is the stale one when the device just changed."""
    try:
        from desk.live_session import _mic_device_resolved, _wasapi_settings
        dev = _mic_device_resolved()
        return dev, _wasapi_settings(dev)
    except Exception as e:
        log.debug("[OFFLINE AUDIO] could not resolve configured mic device: %s", e)
        return None, None


def _offline_output_device():
    """The same configured speaker (speaker_device / NOVA_SPEAKER_DEVICE)
    the cloud path resolves to, with WASAPI auto_convert -- without it, a
    shared-mode device that does not natively run at the TTS sample rate
    raises "Invalid sample rate" instead of resampling. See
    desk.live_session._wasapi_settings for the measurements."""
    try:
        from desk.live_session import _speaker_device, _wasapi_settings
        dev = _speaker_device()
        return dev, _wasapi_settings(dev)
    except Exception as e:
        log.debug("[OFFLINE AUDIO] could not resolve configured speaker device: %s", e)
        return None, None


def _offline_device_label(device, output: bool) -> str:
    try:
        from desk.live_session import _device_label
        return _device_label(device, output)
    except Exception:
        return "system default" if device is None else str(device)


def _open_offline_mic_stream(gate):
    """Open the barge-in mic on a real PortAudio callback, not a blocking
    read loop -- see the module-level note on why that matters. Returns the
    started stream, or None if it could not be opened (barge-in is then
    simply unavailable for this utterance; TTS still plays)."""
    device, extra = _offline_input_device()

    def _cb(indata, frames, time_info, status) -> None:
        try:
            gate.process(np.asarray(indata).reshape(-1))
        except Exception:
            pass

    try:
        stream = sd.InputStream(
            samplerate=_BARGE_IN_MIC_RATE, channels=1, dtype="int16",
            blocksize=1024, callback=_cb, device=device, extra_settings=extra,
        )
        stream.start()
        if not _offline_devices_logged.is_set():
            log.info("[OFFLINE AUDIO] barge-in mic: %s",
                     _offline_device_label(device, False))
        return stream
    except Exception as e:
        log.warning("[OFFLINE AUDIO] barge-in mic open failed (%s): %s -- "
                    "TTS will still play, without interruption support",
                    _offline_device_label(device, False), e)
        return None


def _play_pcm_with_barge_in(audio_i16: np.ndarray, samplerate: int) -> bool:
    """Play mono int16 *audio_i16*, stoppable by a real voice interruption.

    Returns True if the user actually barged in. The mic runs concurrently
    with playback rather than being polled between chunks: interruption has
    to be heard while it is happening, not after the fact.
    """
    gate = _get_offline_gate()
    _offline_barge_event.clear()
    gate.set_speaking(True)

    mic_stream = _open_offline_mic_stream(gate)

    interrupted = False
    chunk = 1024
    chunks_written = 0
    out_device, out_extra = _offline_output_device()
    try:
        with sd.OutputStream(samplerate=samplerate, channels=1, dtype="int16",
                             device=out_device, extra_settings=out_extra) as out:
            if not _offline_devices_logged.is_set():
                log.info("[OFFLINE AUDIO] TTS output: %s (%d Hz)",
                         _offline_device_label(out_device, True), samplerate)
                _offline_devices_logged.set()
            for i in range(0, len(audio_i16), chunk):
                block = audio_i16[i:i + chunk]
                # Fed at the moment it is written, not at enqueue time -- the
                # echo canceller aligns against when the room actually hears
                # it. See the identical comment in desk/live_session.py.
                gate.reference(block.tobytes(), rate=samplerate)
                out.write(block.reshape(-1, 1))
                chunks_written += 1
                if _offline_barge_event.is_set():
                    interrupted = True
                    break
    except Exception as e:
        # Logged with the device/rate that failed, then re-raised: the
        # caller (_speak_pyttsx3/_speak_piper) must see this as a failure
        # and fall through to the next TTS engine, not report success on an
        # output stream that never actually opened.
        log.error("[OFFLINE AUDIO] TTS output stream failed (%s, %d Hz): %s",
                  _offline_device_label(out_device, True), samplerate, e)
        raise
    finally:
        if mic_stream is not None:
            try:
                mic_stream.stop()
                mic_stream.close()
            except Exception:
                pass
        gate.set_speaking(False, interrupted=interrupted)

    total_chunks = max(1, -(-len(audio_i16) // chunk))
    log.debug("[OFFLINE AUDIO] played %d/%d chunk(s), interrupted=%s",
             chunks_written, total_chunks, interrupted)
    if interrupted:
        print("   … stopped (barge-in)", flush=True)
    return interrupted

def speak_offline(text: str, block: bool = False) -> None:
    """
    Queue text for speaking. Non-blocking by default.
    If block=True, waits until speech completes.
    """
    print(f"\n🔊 NOVA: {text}\n")
    try:
        _nova._broadcast_ui({"type": "nova_speak", "text": text})
    except Exception:
        pass
    
    done_event = threading.Event()
    _tts_queue.put((text, done_event))
    
    if block:
        done_event.wait()

def _speak_offline_impl(text: str) -> None:
    if not _speak_pyttsx3(text):
        if not _speak_piper(text):
            log.warning("TTS failed — text only.")


#: NOVA's voice identity. Gemini Live uses "Aoede" -- a female voice -- and
#: that is now part of who NOVA sounds like; a local fallback should not
#: turn her into what reads as a different, male assistant just because the
#: network disappeared. Centralized here rather than picked ad hoc per
#: engine.
NOVA_VOICE_GENDER = "female"

#: Name substrings that mark a voice as female when the engine/driver does
#: not expose a gender attribute pyttsx3 can read.
_FEMALE_NAME_HINTS = ("zira", "female", "eva", "susan", "hazel", "aria",
                     "jenny", "samantha")

_offline_voice_logged = threading.Event()


def _select_pyttsx3_voice(voices):
    """Best available English voice matching NOVA_VOICE_GENDER, and its
    name for diagnostics.

    Not "the first voice whose name contains 'english'": on a machine with
    Microsoft David (male) enumerated before Zira (female), that always
    picked David -- which is exactly why offline NOVA sounded like a
    different, male assistant from the cloud one.
    """
    english = []
    for v in voices:
        name = getattr(v, "name", None)
        vid = getattr(v, "id", None)
        if not isinstance(name, str) or not isinstance(vid, str):
            continue
        langs = getattr(v, "languages", None) or []
        is_english = ("english" in name.lower()
                     or any("en" in str(lang).lower() for lang in langs))
        if is_english:
            gender = str(getattr(v, "gender", "") or "").lower()
            english.append((vid, name, gender))

    if not english:
        return None, ""

    def _matches_target_gender(entry) -> bool:
        _vid, name, gender = entry
        if gender:
            return NOVA_VOICE_GENDER in gender
        return any(hint in name.lower() for hint in _FEMALE_NAME_HINTS)

    matched = [e for e in english if _matches_target_gender(e)]
    chosen_id, chosen_name, _ = (matched or english)[0]
    return chosen_id, chosen_name


def _speak_pyttsx3(text: str) -> bool:
    if pyttsx3 is None:
        return False
    try:
        engine = pyttsx3.init()
        engine.setProperty("rate", TTS_RATE)
        engine.setProperty("volume", TTS_VOLUME)
        voices = engine.getProperty("voices")
        if voices is None:
            voices = []
        elif not isinstance(voices, (list, tuple, set)):
            voices = [voices]
        vid, vname = _select_pyttsx3_voice(voices)
        if vid:
            engine.setProperty("voice", vid)
        if not _offline_voice_logged.is_set():
            log.info("[OFFLINE VOICE] pyttsx3: %s",
                     vname or "engine default (no matching voice found)")
            _offline_voice_logged.set()

        # Rendered to a file rather than played via engine.say()+
        # runAndWait(), so playback goes through the same barge-in-aware
        # path as Piper -- one stream the gate can be fed a reference from
        # and stop early, instead of a playback call neither the mic thread
        # nor anything else can interrupt.
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            engine.save_to_file(text, tmp_path)
            engine.runAndWait()
            engine.stop()
            rate, audio = wav_write.read(tmp_path)
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass

        if audio.ndim > 1:
            audio = audio[:, 0]
        if audio.dtype != np.int16:
            audio = audio.astype(np.int16)
        if len(audio) == 0:
            return False
        _play_pcm_with_barge_in(audio, rate)
        return True
    except Exception as e:
        log.error(f"pyttsx3: {e}")
        return False

def _speak_piper(text: str) -> bool:
    if not os.path.exists(PIPER_MODEL) or not PIPER_MODEL.endswith(".onnx"):
        return False
    try:
        result = subprocess.run(
            [sys.executable, "-m", "piper", "--model", PIPER_MODEL, "--output-raw"],
            input=text.encode(), capture_output=True, timeout=15, shell=False,
        )
        if result.returncode != 0 or not result.stdout:
            return False
        audio = np.frombuffer(result.stdout, dtype=np.int16)
        if len(audio) == 0:
            return False
        _play_pcm_with_barge_in(audio, PIPER_RATE)
        return True
    except Exception as e:
        log.error(f"Piper: {e}")
        return False

# ─── OFFLINE STT — UNIFIED MIC LOOP (same as online) ─────────────────────────

#: Seconds to let a room's echo of NOVA's own TTS decay before trusting raw
#: volume-threshold VAD again. listen_offline() opens a brand new mic stream
#: independent of the barge-in gate above (VoiceGate's own SPEAK_COOLDOWN_S
#: only governs *its* stream, not this one), so without an explicit pause
#: here the tail of NOVA's last sentence -- still audible for a moment after
#: playback stops, especially on a device without real acoustic isolation
#: from the speaker -- can register as the user talking.
LISTEN_SETTLE_S = 0.4

#: Fraction of recorded chunks that must be "loud" for a capture to count as
#: real speech rather than scattered noise transients. min_speech_r alone
#: only requires the *count* to accumulate somewhere in the recording, with
#: no requirement that it be continuous -- occasional spikes spread across a
#: long, mostly-silent recording would satisfy it.
MIN_SPEECH_DENSITY = 0.35


def listen_offline():
    """
    Offline listening with proper state management.
    Uses the same calibrated threshold approach as online mode.

    Returns:
        the transcript, if NOVA understood something;
        "" if a real utterance was captured but STT produced no usable
            text (the only case that should prompt "I didn't catch that");
        None for everything else -- true silence, a busy state, a mic
            failure, or the STT subsystem not being ready. None of those
            are the user having said something NOVA failed to hear, and
            speaking as though they were is what turned "the room is quiet"
            into a loop of "I didn't catch that" answered by more silence.
    """
    global _offline_state

    if get_offline_state() == OfflineState.SPEAKING:
        # Don't listen while speaking
        return None

    if LISTEN_SETTLE_S:
        time.sleep(LISTEN_SETTLE_S)

    set_offline_state(OfflineState.LISTENING)

    fs = 16000
    chunk_size = 1024
    threshold = _nova.AMBIENT_THRESHOLD or DEFAULT_THRESHOLD
    silence_limit = 1.8
    min_speech = 0.6
    max_record = 12.0

    max_silent = int((fs / chunk_size) * silence_limit)
    min_speech_r = int((fs / chunk_size) * min_speech)
    max_total = int((fs / chunk_size) * max_record)

    print(f"\n🎙️  LISTENING... (threshold: {threshold:.4f})")
    device, extra = _offline_input_device()
    log.debug("[OFFLINE AUDIO] mic: %s, threshold=%.4f",
             _offline_device_label(device, False), threshold)

    recorded = []
    silent = 0
    speech = 0
    total_chunks = 0
    first_speech_i = -1
    last_speech_i = -1
    is_recording = False

    try:
        with sd.InputStream(samplerate=fs, channels=1, dtype="float32",
                            blocksize=chunk_size, device=device,
                            extra_settings=extra) as stream:
            while True:
                chunk, _ = stream.read(chunk_size)
                vol = float(np.sqrt(np.mean(chunk ** 2)))
                total_chunks += 1

                # State visualization
                if vol > threshold:
                    if not is_recording:
                        is_recording = True
                        print("🟢 SPEECH DETECTED")
                    if first_speech_i < 0:
                        first_speech_i = total_chunks
                    last_speech_i = total_chunks
                    speech += 1
                    silent = 0
                    recorded.append(chunk.copy())
                else:
                    if is_recording:
                        recorded.append(chunk.copy())
                        silent += 1
                    else:
                        # Still buffering pre-speech for context
                        recorded.append(chunk.copy())
                        if len(recorded) > int((fs / chunk_size) * 0.5):  # Keep 0.5s buffer
                            recorded.pop(0)

                # Check end conditions
                if silent >= max_silent and speech >= min_speech_r:
                    print("🔇 Silence detected, processing...")
                    break
                if len(recorded) >= max_total and is_recording:
                    print("⏱️ Max recording time reached")
                    break

    except Exception as e:
        print(f"❌ Mic error: {e}")
        log.warning("[OFFLINE AUDIO] mic error (%s): %s",
                    _offline_device_label(device, False), e)
        set_offline_state(OfflineState.IDLE)
        return None

    # Density over the *span* containing the detected speech, not the whole
    # recording -- every recording ends with ~1.8s of mandatory silence
    # (that's how the loop knows speech ended), which would otherwise dilute
    # even a real, continuous utterance's ratio toward zero as it goes on.
    span = (last_speech_i - first_speech_i + 1) if first_speech_i >= 0 else 0
    density = (speech / span) if span else 0.0
    log.debug("[OFFLINE AUDIO] capture: chunks=%d speech_chunks=%d "
             "span=%d density=%.2f duration=%.2fs", total_chunks, speech,
             span, density, total_chunks * chunk_size / fs)

    if speech < min_speech_r:
        print("🔇 No speech detected.")
        set_offline_state(OfflineState.IDLE)
        return None

    if density < MIN_SPEECH_DENSITY:
        # Loud chunks accumulated, but scattered thinly across the span they
        # occupy rather than one continuous burst -- noise, not an utterance.
        print("🔇 Noise, not speech (too sparse).")
        log.info("[OFFLINE AUDIO] discarded capture: density=%.2f below "
                 "%.2f (scattered noise, not continuous speech)",
                 density, MIN_SPEECH_DENSITY)
        set_offline_state(OfflineState.IDLE)
        return None

    # Process audio
    audio = np.concatenate(recorded, axis=0)
    audio_i16 = (audio * 32767).astype(np.int16)

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name
        # `wav_write` is the scipy.io.wavfile *module* (see the import at the
        # top of this file), not its write() function -- calling it directly
        # raised "'module' object is not callable" every single time, i.e.
        # every time listen_offline() actually captured real speech, and
        # that raise was outside the try/except below, so it killed the
        # whole offline loop uncaught.
        wav_write.write(tmp_path, fs, audio_i16)

    print("🔍 Transcribing with faster-whisper...")
    set_offline_state(OfflineState.THINKING)

    try:
        if not _stt_loaded.wait(timeout=10):
            print("⚠️  Whisper not loaded yet — skipping.")
            log.warning("[OFFLINE AUDIO] STT subsystem not ready "
                        "(model still loading) -- not the user's silence")
            return None
        with _stt_model_lock:
            if _nova._stt_model is None:
                log.warning("[OFFLINE AUDIO] STT model unavailable -- "
                            "not the user's silence")
                return None
            segments, _ = _nova._stt_model.transcribe(
                tmp_path,
                language="en",
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500),
                condition_on_previous_text=False,
            )
            transcript = " ".join([seg.text.strip() for seg in segments]).strip()
            print(f"📝 Heard: {transcript}")
            log.debug("[OFFLINE AUDIO] STT result: %d chars", len(transcript))
            return transcript
    except Exception as e:
        print(f"⚠️  Transcription error: {e}")
        log.warning("[OFFLINE AUDIO] transcription error: %s", e)
        return None
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass

# ─── OFFLINE TOOL SYSTEM ───────────────────────────────────────────────────────

# Tool definitions in Ollama format
OLLAMA_TOOLS = []

def _build_ollama_tools():
    """Convert tool declarations to Ollama-compatible format."""
    tools = []
    for decl in TOOL_DECLARATIONS:
        if decl["name"] == "remember_fact":
            continue  # Skip memory tools for now
            
        tool = {
            "type": "function",
            "function": {
                "name": decl["name"],
                "description": decl.get("description", ""),
                "parameters": {
                    "type": "object",
                    "properties": decl.get("parameters", {}).get("properties", {}),
                    "required": decl.get("parameters", {}).get("required", [])
                }
            }
        }
        tools.append(tool)
    return tools

# ─── OFFLINE WIKIPEDIA (ZIM) ─────────────────────────────────────────────────

# <style>/<script> bodies are not markup the tag-stripper can remove — their
# *contents* survive it — so a Wikipedia article otherwise arrives as a wall of
# CSS before the first sentence of prose.
_ZIM_DROP_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
_ZIM_TAG_RE = re.compile(r"<[^>]+>")
_ZIM_WS_RE = re.compile(r"\s+")
_ZIM_P_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.I | re.S)


def _zim_text(raw: Any, limit: int = 2000) -> str:
    """Turn a ZIM item's payload into readable plain text.

    libzim returns ``Item.content`` as a memoryview of the raw article bytes —
    HTML, not text. Slicing that straight into the result dict produced
    "<memory at 0x...>" once it was rendered or JSON-encoded, so offline
    articles were unusable even when the lookup itself succeeded.
    """
    try:
        data = bytes(raw)
    except Exception:
        return ""
    html = data.decode("utf-8", errors="replace")
    html = _ZIM_DROP_RE.sub(" ", html)

    # Prefer the article's paragraphs. Wikipedia ZIM pages carry inline
    # TemplateStyles CSS that survives naive tag-stripping, so a whole-document
    # strip returns a wall of ".mw-parser-output{...}" before any prose.
    paragraphs = [
        _ZIM_WS_RE.sub(" ", _ZIM_TAG_RE.sub(" ", m)).strip()
        for m in _ZIM_P_RE.findall(html)
    ]
    prose = " ".join(x for x in paragraphs if x)
    if not prose:
        prose = _ZIM_WS_RE.sub(" ", _ZIM_TAG_RE.sub(" ", html)).strip()
    # ZIM stores raw HTML, so entities survive tag-stripping as literal
    # "&nbsp;" / "&amp;" in what the model is asked to read.
    return _html.unescape(prose)[:limit]


class OfflineWiki:
    """Offline Wikipedia using ZIM files."""
    
    def __init__(self, data_path: str = ZIM_DATA_PATH):
        self.data_path = Path(data_path)
        self.zim_files = list(self.data_path.glob("*.zim"))
        self.libzim_available = False
        
        try:
            import libzim
            archive_cls = getattr(libzim, "Archive", None) or getattr(libzim, "ZimFile", None)
            if archive_cls is None:
                log.warning("libzim does not expose Archive or ZimFile. Offline Wikipedia unavailable.")
                self.libzim_available = False
                self._readers = {}
                return

            self.libzim_available = True
            self._readers = {}
            for zim_file in self.zim_files:
                try:
                    self._readers[zim_file.stem] = archive_cls(str(zim_file))
                except Exception as e:
                    log.warning(f"Failed to load ZIM {zim_file}: {e}")
        except ImportError:
            log.warning("libzim not installed. Offline Wikipedia unavailable.")
            log.warning("Install: pip install libzim")
    
    def search(self, query: str, limit: int = 3) -> List[Dict[str, str]]:
        """Search offline Wikipedia."""
        if not self.libzim_available or not self._readers:
            return []
        
        results = []
        
        for name, reader in self._readers.items():
            try:
                # Try exact entry first
                entry = reader.get_entry_by_path(query.replace(" ", "_"))
                if entry:
                    content = _zim_text(entry.get_item().content)
                    if content:
                        results.append({
                            "source": f"Wikipedia ({name})",
                            "title": query,
                            "content": content,
                        })
                    continue
                
                # Fallback: search suggestions (limited in libzim)
                suggestions = list(reader.get_suggestions(query, limit=limit))
                for suggestion in suggestions:
                    entry = reader.get_entry_by_path(suggestion[0])
                    if entry:
                        content = _zim_text(entry.get_item().content)
                        if content:
                            results.append({
                                "source": f"Wikipedia ({name})",
                                "title": str(suggestion[0]).replace("_", " "),
                                "content": content,
                            })
                        
            except Exception as e:
                log.debug(f"ZIM search error: {e}")
                continue
        
        return results

_offline_wiki = None

def get_offline_wiki() -> OfflineWiki:
    global _offline_wiki
    if _offline_wiki is None:
        _offline_wiki = OfflineWiki()
    return _offline_wiki

# ─── OFFLINE MAPS ──────────────────────────────────────────────────────────────

class OfflineMaps:
    """Offline maps using OpenStreetMap data (MBTiles or OSM.PBF)."""
    
    def __init__(self, maps_path: str = OFFLINE_MAPS_PATH):
        self.maps_path = Path(maps_path)
        self.mbtiles_files = list(self.maps_path.glob("*.mbtiles"))
        self.has_maps = len(self.mbtiles_files) > 0
        
        if not self.has_maps:
            log.info("No offline maps found. Place .mbtiles files in " + maps_path)
    
    def search_location(self, query: str) -> List[Dict[str, Any]]:
        """Search for locations in offline map data."""
        if not self.has_maps:
            return []
        
        results = []
        try:
            import sqlite3
            for mbtile in self.mbtiles_files:
                conn = sqlite3.connect(str(mbtile))
                cursor = conn.cursor()
                
                # Search in MBTiles metadata and tiles
                cursor.execute("""
                    SELECT zoom_level, tile_column, tile_row, tile_data 
                    FROM tiles 
                    LIMIT 1
                """)
                
                # For proper geocoding, we'd need a spatialite extension
                # This is a basic implementation
                results.append({
                    "source": str(mbtile.name),
                    "query": query,
                    "note": "Basic offline map search. For full geocoding, install spatialite."
                })
                conn.close()
        except Exception as e:
            log.error(f"Offline map search error: {e}")
        
        return results

_offline_maps = None

def get_offline_maps() -> OfflineMaps:
    global _offline_maps
    if _offline_maps is None:
        _offline_maps = OfflineMaps()
    return _offline_maps

# ─── OFFLINE TOOL EXECUTION ──────────────────────────────────────────────────

def _execute_offline_tool(name: str, args: Dict[str, Any], meta: dict) -> str:
    """Execute tools in offline mode with offline-aware fallbacks."""
    
    # Offline Wikipedia fallback for web_search
    if name == "web_search":
        query = args.get("query", "")
        wiki_results = get_offline_wiki().search(query)
        if wiki_results:
            return json.dumps({
                "offline": True,
                "source": "Wikipedia (ZIM)",
                "results": wiki_results
            }, indent=2)
        return "No internet and no offline Wikipedia data available. I can't search for that right now."
    
    # Offline maps fallback for location queries
    if name in ["get_location", "find_place", "navigate"]:
        query = args.get("query", args.get("destination", ""))
        map_results = get_offline_maps().search_location(query)
        if map_results:
            return json.dumps({
                "offline": True,
                "source": "Offline Maps",
                "results": map_results
            }, indent=2)
        return "No internet and no offline map data available for that location."
    
    # File operations work offline
    if name in ["read_file", "write_file", "list_directory", "run_shell"]:
        return _execute_tool_sync(name, args, meta)
    
    # Self-editing works offline
    if name == "self_editor":
        return _execute_tool_sync(name, args, meta)
    
    # Planner works offline
    if name == "planner":
        return _execute_tool_sync(name, args, meta)
    
    # Memory works offline
    if name == "remember_fact":
        return _execute_tool_sync(name, args, meta)
    
    # For tools that absolutely need internet
    internet_required = ["web_search", "send_email", "get_weather", "download_file"]
    if name in internet_required:
        return f"❌ Tool '{name}' requires internet connection. Currently offline."
    
    # Default: try anyway
    return _execute_tool_sync(name, args, meta)

# ─── OFFLINE LLM CALLERS ─────────────────────────────────────────────────────

def _call_ollama_v2(
    model: str,
    messages: List[Dict[str, Any]],
    use_tools: bool = True,
) -> Optional[Dict[str, Any]]:
    """
    v2: Fixed Ollama tool calling.
    Ollama uses 'tools' array with specific format, not OpenAI format.
    """
    timeout = OFFLINE_TIMEOUTS.get(model, 20)
    
    try:
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": 0.7,
                "num_predict": 1024,
            }
        }
        
        # Only add tools for capable models
        if use_tools and model not in ["tinyllama"]:
            tools = _build_ollama_tools()
            if tools:
                payload["tools"] = tools
        
        r = requests.post(
            "http://localhost:11434/api/chat",
            json=payload,
            timeout=timeout
        )
        r.raise_for_status()
        data = r.json()
        message = data.get("message", {})
        
        # Parse tool calls from Ollama response
        tool_calls = []
        raw_tool_calls = message.get("tool_calls") or []
        
        for tc in raw_tool_calls:
            if isinstance(tc, dict) and "function" in tc:
                func = tc["function"]
                args = func.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                tool_calls.append({
                    "id": tc.get("id") or str(int(time.time())),
                    "name": func.get("name", ""),
                    "args": args,
                })
        
        return {
            "text": message.get("content", "").strip(),
            "tool_calls": tool_calls,
            "raw_message": message,
        }
        
    except requests.exceptions.Timeout:
        log.warning(f"Ollama/{model} timed out after {timeout}s.")
        return None
    except requests.exceptions.ConnectionError:
        log.warning(f"Ollama/{model} not running. Start with: ollama serve")
        return None
    except Exception as e:
        log.warning(f"Ollama/{model} failed: {e}")
        return None


def _get_offline_response_v2(
    messages: List[Dict[str, Any]],
    use_tools: bool = True,
) -> Optional[Dict[str, Any]]:
    """
    v2: Fixed offline brain with proper tool handling.
    Priority: Intelligence Router → Gemini REST → Ollama with tools → Ollama without tools.
    """
    # Try the intelligence router first (unified online/offline)
    try:
        import nova
        router = getattr(nova, "_nova_router", None)
        if router:
            result = router.complete(
                messages=messages,
                system=NOVA_OFFLINE_PROMPT,
                tools=TOOL_DECLARATIONS if use_tools else None,
            )
            if result.ok:
                return {"text": result.text, "tool_calls": result.tool_calls}
    except Exception as e:
        log.debug("router failed, falling back to legacy cascade: %s", e)

    # Legacy fallback: Try Gemini first if online
    if is_online() and HAS_GEMINI and GEMINI_API_KEY:
        print("🌐 [Gemini REST]...")
        result = _call_gemini_chat(messages, use_tools=use_tools)
        if result:
            return result
        print("⚠️  Gemini failed → local brain...")
    
    print("📴 Offline — using local brain...")
    
    if not is_ollama_running():
        print("❌ Ollama not running. Start: ollama serve")
        return None
    
    # Try models with tools first
    for model in OFFLINE_MODELS:
        if model == "tinyllama":
            continue  # Skip tinyllama for tool calls
            
        timeout = OFFLINE_TIMEOUTS.get(model, 20)
        print(f"🔌 [{model}] with tools... (max {timeout}s)")
        
        # Use slim prompt for offline models
        slim = [{"role": "system", "content": NOVA_OFFLINE_PROMPT}] + messages[1:]
        result = _call_ollama_v2(model, slim, use_tools=True)
        
        if result:
            print(f"✅ {model} responded.")
            return result
        print(f"⚠️  {model} failed or timed out.")
    
    # Fallback: try without tools
    for model in OFFLINE_MODELS:
        timeout = OFFLINE_TIMEOUTS.get(model, 20)
        print(f"🔌 [{model}] conversation only... (max {timeout}s)")
        
        slim = [{"role": "system", "content": NOVA_OFFLINE_PROMPT}] + messages[1:]
        result = _call_ollama_v2(model, slim, use_tools=False)
        
        if result:
            print(f"✅ {model} responded (no tools).")
            return result
        print(f"⚠️  {model} failed.")
    
    return None

# ─── OFFLINE THINKING — FIXED AGENT HIJACKING ───────────────────────────────

def think_offline_v2(user_message: str, meta: dict) -> str:
    """
    v2: Fixed offline thinking.
    - Agent only handles specific dev tasks, not everything
    - Proper tool execution with offline fallbacks
    - Memory integration
    """
    
    # [FIX] Agent only for specific patterns, not hijacking
    dev_patterns = [
        "write code", "create file", "edit file", "fix bug", 
        "debug", "refactor", "code review", "implement"
    ]
    is_dev_request = any(p in user_message.lower() for p in dev_patterns)
    
    if is_dev_request:
        agent_result = agent_process(user_message, meta)
        if agent_result and "DEV AGENT" not in agent_result:
            # Agent actually did something useful
            _nova.conversation_history.append({"role": "user", "content": user_message})
            _nova.conversation_history.append({"role": "assistant", "content": agent_result})
            _trim_history()
            return agent_result
    
    # Normal LLM flow
    _nova.conversation_history.append({"role": "user", "content": user_message})
    
    # Build memory context
    mem_ctx = build_memory_context(meta, query=user_message)
    sys_content = NOVA_OFFLINE_PROMPT
    if mem_ctx:
        sys_content += f"\n\nMEMORY:\n{mem_ctx}"
    
    # Add offline capability awareness
    sys_content += "\n\nOFFLINE CAPABILITIES: You have access to offline Wikipedia (ZIM) and offline maps. Use them when relevant."
    
    messages = [{"role": "system", "content": sys_content}] + _nova.conversation_history
    
    # Get LLM response
    result = _get_offline_response_v2(messages, use_tools=True)
    
    if result is None:
        err = "All brain engines offline. Check your connection or start Ollama."
        _nova.conversation_history.append({"role": "assistant", "content": err})
        _trim_history()
        return err
    
    # Handle tool calls
    if result["tool_calls"]:
        print(f"🔧 Tool calls detected: {[tc['name'] for tc in result['tool_calls']]}")
        
        # Execute each tool and collect results as plain-text context. Feeding
        # tool output back as text avoids the OpenAI<->Gemini function-call
        # round-trip, which the converter does not support.
        context_bits = []
        for tc in result["tool_calls"]:
            tool_name = tc["name"]
            tool_args = tc["args"]
            
            print(f"   Executing: {tool_name}({json.dumps(tool_args)[:100]})")
            
            try:
                tool_result = _execute_offline_tool(tool_name, tool_args, meta)
            except Exception as tool_err:
                tool_result = f"[{tool_name} failed: {tool_err}]"
            tool_result = tool_result if isinstance(tool_result, str) else str(tool_result)
            print(f"   Result: {tool_result[:200]}...")
            
            context_bits.append(
                f"Tool '{tool_name}' returned:\n{tool_result[:500]}"
            )
        
        tool_prompt = (
            "The following tool results were retrieved for the user's last message.\n\n"
            + "\n\n".join(context_bits)
            + "\n\nAnswer the user clearly using these results. If the results are "
              "empty or insufficient, say so honestly and do not fabricate."
        )
        followup = list(messages) + [
            {"role": "assistant", "content": (result.get("text") or "").strip()},
            {"role": "user", "content": tool_prompt},
        ]
        
        # Get final response after tool execution
        final = _get_offline_response_v2(followup, use_tools=False)
        text = final["text"].strip() if final and final["text"].strip() else "Done."
        
        _nova.conversation_history.append({"role": "assistant", "content": text})
        _trim_history()
        return text
    
    # No tool calls, just text
    text = result["text"].strip() or "I couldn't generate a response."
    _nova.conversation_history.append({"role": "assistant", "content": text})
    _trim_history()
    return text

# ─── OFFLINE LOOP — OVERHAULED ─────────────────────────────────────────────────

def run_offline_loop_v2(meta: dict) -> None:
    """
    v2: Fixed offline loop with:
    - Proper state machine
    - Non-blocking TTS
    - Better mic handling
    - Tool support
    - Offline Wikipedia/Maps
    """
    global user_name
    
    print("\n[NOVA] 🔌 Offline mode v2.0")
    print("       Tools: " + ("✅" if is_ollama_running() else "❌"))
    print("       Wiki:  " + ("✅" if get_offline_wiki().libzim_available else "❌"))
    print("       Maps:  " + ("✅" if get_offline_maps().has_maps else "❌"))
    
    # Set up planner speak callback
    if nova_state._planner is not None:
        nova_state._planner.set_speak(speak_offline)
    
    # Load STT
    if not TEXT_MODE:
        _calibrate = _nova_get("calibrate_ambient_noise")
        if callable(_calibrate):
            calibrate_ambient_noise = _calibrate
            calibrate_ambient_noise()
        else:
            calibrate_ambient_noise = None
    
    # Greeting
    _input_fn = get_text_input if TEXT_MODE else listen_offline
    _greet = offline_greeting
    if callable(_greet):
        user_name = _greet(meta, speak_offline, _input_fn)
    else:
        user_name = meta.get("user_name") or "there"
        speak_offline(f"Welcome back, {user_name}." if meta.get("user_name") else f"Hello, {user_name}.")
    
    # Main loop
    _consecutive_misses = 0
    while True:
        # Check state
        if get_offline_state() == OfflineState.SPEAKING:
            time.sleep(0.1)
            continue

        # Get input
        set_offline_state(OfflineState.LISTENING)
        user_input = (
            get_text_input() if TEXT_MODE and callable(get_text_input)
            else listen_offline() if callable(listen_offline)
            else input("You: ") if TEXT_MODE
            else ""
        )

        # Network recovery check
        _recover_fn = _nova_get("check_network_recovery")
        if callable(_recover_fn) and _recover_fn(
            meta, speak_offline,
            _input_fn if callable(_input_fn) else (lambda: input("You: ")),
            GEMINI_API_KEY or "", FORCE_OFFLINE
        ):
            return

        if user_input is None:
            # Nothing to react to: silence, a mic hiccup, or the STT
            # subsystem not being ready (listen_offline() logs which, via
            # log.* rather than print(), so it actually reaches nova.log).
            # None of those are the user having said something NOVA failed
            # to hear -- speaking as though they were is what turned "the
            # room is quiet" into a loop of "I didn't catch that" answered
            # by more silence, which itself can register as more "speech".
            time.sleep(0.5)  # prevent CPU-pinning busy-loop on repeated mic/input failure
            continue

        if user_input == "":
            # A real utterance was captured and STT genuinely produced
            # nothing usable -- the one case "I didn't catch that" belongs
            # to. Not on every consecutive occurrence, though: a genuinely
            # broken STT path must not become the same loop with extra
            # steps, which is exactly what unconditionally repeating it did.
            _consecutive_misses += 1
            if not TEXT_MODE:
                if _consecutive_misses <= 2:
                    speak_offline("I didn't catch that.")
                elif _consecutive_misses == 3:
                    speak_offline("I'm having trouble understanding you -- "
                                  "you can type instead if that's easier.")
                else:
                    log.info("[OFFLINE] %d consecutive unintelligible "
                             "captures -- no longer repeating "
                             "\"I didn't catch that\"", _consecutive_misses)
            time.sleep(0.5)
            continue

        _consecutive_misses = 0
        print(f"👤 You: {user_input}")
        set_offline_state(OfflineState.PROCESSING)
        
        lower = user_input.lower()
        
        # Memory queries
        if any(p in lower for p in ["what do you remember", "what do you know about me"]):
            speak_offline(get_all_memory_text(meta))
            set_offline_state(OfflineState.IDLE)
            continue
        
        # Memory storage
        handled = False
        for phrase in ["remember that", "don't forget", "note that", "keep in mind", "remember"]:
            if lower.startswith(phrase):
                fact = user_input[len(phrase):].strip()
                add_memory_fact(fact, meta)
                speak_offline("Got it. Noted.")
                handled = True
                break
        if handled:
            set_offline_state(OfflineState.IDLE)
            continue
        
        # ── Tier 5 & 6 REPL commands ────────────────────────────────────────
        if user_input.startswith("/"):
            _handled = False
            # Heartbeat commands
            try:
                from nova_heartbeat import handle_heartbeat_command
                if _nova._heartbeat is not None:
                    _hb_result = handle_heartbeat_command(user_input, _nova._heartbeat)
                    if _hb_result is not None:
                        speak_offline(_hb_result)
                        set_offline_state(OfflineState.IDLE)
                        _handled = True
            except (ImportError, Exception):
                pass
            # Audit command
            if not _handled and user_input == "/audit":
                try:
                    from nova_safety import get_audit_log
                    speak_offline(get_audit_log(last_n=10))
                except ImportError:
                    speak_offline("nova_safety.py not installed.")
                _handled = True
            if not _handled and user_input == "/cost":
                try:
                    from nova_safety import get_cost_summary
                    speak_offline(get_cost_summary())
                except ImportError:
                    speak_offline("nova_safety.py not installed.")
                _handled = True
            if not _handled and user_input in ("/memory", "/mem", "/memories"):
                try:
                    _lm = nova_state._living_memory
                    if _lm:
                        speak_offline(_lm.exec_command("stats"))
                    else:
                        speak_offline("Living memory not initialized.")
                except Exception:
                    speak_offline("Living memory error.")
                _handled = True
            if not _handled and user_input in ("/tasks demo", "/task demo", "/demo task"):
                try:
                    _tm = nova_state._task_manager
                    if not _tm:
                        speak_offline("Task manager not initialized.")
                    else:
                        steps = [
                            {"tool": "planner", "args": {"action": "list"}, "verify": "pending|reminder|none"},
                            {"tool": "nova_memory", "args": {"cmd": "stats"}, "verify": "memory|active"},
                        ]
                        res = _tm.exec_command(
                            "submit", task_id="", title="Self-demo: living systems",
                            steps=steps, meta={},
                        )
                        speak_offline(f"{res} Try '/tasks' in a moment to watch progress.")
                except Exception:
                    speak_offline("Task manager error.")
                _handled = True
            if not _handled and user_input in ("/tasks", "/task", "/jobs"):
                try:
                    _tm = nova_state._task_manager
                    if _tm:
                        speak_offline(_tm.exec_command("status", {}))
                    else:
                        speak_offline("Task manager not initialized.")
                except Exception:
                    speak_offline("Task manager error.")
                _handled = True
            if _handled:
                continue

        # Exit
        if any(w in lower for w in ["goodbye nova", "shutdown nova", "exit nova", "close nova"]):
            speak_offline(f"Goodbye, {user_name}.")
            break
        
        # Main thinking
        reply = think_offline_v2(user_input, meta)
        speak_offline(reply)
        
        # [living memory] feed finished turn
        _living_turn = getattr(_nova, "_living_turn", None)
        if _living_turn and reply:
            try:
                _living_turn(user_input, reply)
            except Exception:
                pass
        
        # Periodic memory extraction
        _nova._mem_extract_turn_counter += 1
        if _nova._mem_extract_turn_counter % _MEM_EXTRACT_EVERY_N == 0:
            meta = extract_memory_updates(user_input, reply, meta)
        # Deliberately not forcing IDLE here. speak_offline() just queued the
        # reply and returned without waiting for it, so the TTS worker has
        # not necessarily set SPEAKING yet -- setting IDLE here raced it and
        # sometimes won, and the next loop iteration's "if state == SPEAKING:
        # wait" then read IDLE and opened the mic while NOVA was still
        # talking. The worker already owns this transition (SPEAKING ->
        # LISTENING once done); nothing here needs to duplicate it.
# At the bottom of your offline code, add:
run_offline_loop = run_offline_loop_v2  # Alias for compatibility
