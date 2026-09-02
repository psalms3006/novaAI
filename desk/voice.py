"""desk.voice - push-to-talk microphone capture + transcription.

Server-side capture (sounddevice) so it works in the embedded WebView2 window
without WebView2 media-permission quirks. Transcription uses faster-whisper
(local) when available, otherwise the Deepgram REST API when a key is set.
If neither is available the endpoint reports the limitation honestly.

This reuses NOVA's audio stack (sounddevice 16 kHz mono int16) and does not
touch live_extra / NovaLive (which is left running its own always-listening
loop untouched by the desktop app).
"""
from __future__ import annotations

import io
import os
import struct
import threading
import time
import wave

try:
    import sounddevice as sd
    HAS_SOUNDDEVICE = True
except Exception:
    sd = None
    HAS_SOUNDDEVICE = False

import requests

SAMPLE_RATE = 16000
_frames: list = []
_lock = threading.Lock()
_stream = None
_capture_active = False
_start_time = 0.0


def audio_available() -> bool:
    return HAS_SOUNDDEVICE and (sd.query_devices() is not None)


# ── capture ────────────────────────────────────────────────────────────────────

def _callback(indata, frames, time_info, status):
    with _lock:
        if _capture_active:
            _frames.append(bytes(indata))


def start_capture(max_seconds: float = 20.0) -> tuple[bool, str]:
    global _stream, _capture_active, _start_time
    if not HAS_SOUNDDEVICE:
        return False, "No audio device available on this machine."
    if _capture_active:
        return False, "Recording already in progress."
    try:
        _frames.clear()
        _capture_active = True
        _start_time = time.time()
        with _lock:
            pass
        _stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="int16",
            blocksize=1024, callback=_callback,
        )
        _stream.start()
        return True, "Recording"
    except Exception as e:
        _capture_active = False
        return False, f"Could not open the microphone: {e}"


def stop_capture() -> tuple[str | None, str]:
    """Stop recording, return (wav_path_or_None, status_text)."""
    global _stream, _capture_active
    if not _capture_active:
        return None, "No recording in progress."
    try:
        if _stream:
            _stream.stop()
            _stream.close()
    except Exception:
        pass
    _capture_active = False
    with _lock:
        data = b"".join(_frames)
    if len(data) < SAMPLE_RATE * 2 * 1:  # less than ~1s
        return None, "Recording too short — please hold the button a little longer."
    path = _save_wav(data)
    return path, "Recorded"


def abort_capture() -> str:
    global _stream, _capture_active
    try:
        if _stream:
            _stream.stop()
            _stream.close()
    except Exception:
        pass
    _capture_active = False
    with _lock:
        _frames.clear()
    return "Cancelled"


def status() -> dict:
    return {
        "capturing": _capture_active,
        "elapsed": round(time.time() - _start_time, 1) if _capture_active else 0.0,
        "available": audio_available(),
    }


def _save_wav(data: bytes) -> str:
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    with wave.open(tmp.name, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(data)
    return tmp.name


# ── transcription ──────────────────────────────────────────────────────────────

_whisper = None
_whisper_lock = threading.Lock()


def _load_whisper():
    global _whisper
    with _whisper_lock:
        if _whisper is None:
            from faster_whisper import WhisperModel  # lazy heavy import
            size = os.getenv("WHISPER_MODEL_SIZE", "tiny")
            _whisper = WhisperModel(size, device="cpu", compute_type="int8")
    return _whisper


def _transcribe_whisper(path: str) -> str:
    model = _load_whisper()
    segments, _info = model.transcribe(path)
    return " ".join(
        (s.text or "").strip() for s in segments
    ).strip()


def _transcribe_deepgram(path: str) -> str:
    key = os.getenv("DEEPGRAM_API_KEY", "")
    model = os.getenv("DEEPGRAM_STT_MODEL", "nova-2-general")
    url = "https://api.deepgram.com/v1/listen"
    with open(path, "rb") as f:
        audio = f.read()
    resp = requests.post(
        url,
        headers={"Authorization": f"Token {key}"},
        params={"model": model, "smart_format": "true"},
        data=audio,
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Deepgram returned HTTP {resp.status_code}")
    return (resp.json().get("results", {}).get("channels", [{}])[0]
            .get("alternatives", [{}])[0].get("transcript", "")).strip()


def transcription_available() -> dict:
    has_whisper = False
    try:
        import importlib.util
        has_whisper = importlib.util.find_spec("faster_whisper") is not None
    except Exception:
        has_whisper = False
    return {
        "whisper": has_whisper,
        "deepgram": bool(os.getenv("DEEPGRAM_API_KEY", "")),
        "any": has_whisper or bool(os.getenv("DEEPGRAM_API_KEY", "")),
    }


def transcribe(path: str) -> tuple[str | None, str]:
    """Transcribe a WAV. Returns (transcript, error_or_empty)."""
    avail = transcription_available()
    if not avail["any"]:
        return None, (
            "Voice input is not available right now: no local whisper model and "
            "no Deepgram API key configured."
        )
    errors = []
    if avail["whisper"]:
        try:
            text = _transcribe_whisper(path)
            if text:
                return text, ""
        except Exception as e:
            errors.append(f"whisper: {e}")
    if avail["deepgram"]:
        try:
            text = _transcribe_deepgram(path)
            if text:
                return text, ""
        except Exception as e:
            errors.append(f"deepgram: {e}")
    return None, "Could not transcribe audio. " + " ".join(errors)