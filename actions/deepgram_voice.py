"""
Deepgram Voice Integration for NOVA
Replaces: Whisper (STT) + pyttsx3/Piper (TTS)
Features: Nova-3 STT, Aura-2 TTS, real-time streaming
"""

import os
import io
import queue
import threading
import time
import logging
from typing import Optional, Callable, Generator
from pathlib import Path

import numpy as np
import sounddevice as sd
from deepgram import DeepgramClient
from deepgram.core.events import EventType

log = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
STT_MODEL = os.getenv("DEEPGRAM_STT_MODEL", "nova-3-general")
TTS_MODEL = os.getenv("DEEPGRAM_TTS_MODEL", "aura-2-asteria-en")
SAMPLE_RATE = 16000  # Deepgram expects 16kHz for STT
CHUNK_DURATION = 0.1  # 100ms chunks

# ── Global State ─────────────────────────────────────────────────────────────
_dg_client: Optional[DeepgramClient] = None
_tts_queue: queue.Queue = queue.Queue()
_tts_thread: Optional[threading.Thread] = None
_tts_stop_event = threading.Event()


# ════════════════════════════════════════════════════════════════════════════
#  INITIALIZATION
# ════════════════════════════════════════════════════════════════════════════

def init_deepgram() -> bool:
    """Initialize Deepgram client. Returns True if ready."""
    global _dg_client
    if not DEEPGRAM_API_KEY:
        log.error("DEEPGRAM_API_KEY not set in .env")
        return False
    try:
        _dg_client = DeepgramClient(DEEPGRAM_API_KEY)
        log.info("Deepgram client initialized")
        return True
    except Exception as e:
        log.error(f"Deepgram init failed: {e}")
        return False


# ════════════════════════════════════════════════════════════════════════════
#  SPEECH-TO-TEXT (STT) — Replaces Whisper
# ════════════════════════════════════════════════════════════════════════════

def listen_deepgram(
    timeout: float = 30.0,
    silence_threshold: float = 1.5,
    on_interim: Optional[Callable[[str], None]] = None
) -> str:
    """
    Record audio and transcribe with Deepgram Nova-3.
    Much faster than Whisper (under 300ms latency).

    Args:
        timeout: Max recording duration in seconds
        silence_threshold: Seconds of silence to stop recording
        on_interim: Optional callback for interim (partial) results

    Returns:
        Final transcript string
    """
    if _dg_client is None:
        if not init_deepgram():
            return "[Deepgram not available]"

    transcript = ""
    final_received = threading.Event()
    audio_buffer = io.BytesIO()

    def on_message(result):
        nonlocal transcript
        if result.type == "Results":
            alt = result.channel.alternatives[0]
            if alt.transcript:
                if result.is_final:
                    transcript += alt.transcript + " "
                    final_received.set()
                elif on_interim:
                    on_interim(alt.transcript)

    try:
        with _dg_client.listen.v2.connect(
            model=STT_MODEL,
            encoding="linear16",
            sample_rate=SAMPLE_RATE,
            channels=1,
            punctuate=True,
            smart_format=True,
            interim_results=True,
            endpointing=300,  # 300ms silence = end of utterance
        ) as connection:

            connection.on(EventType.OPEN, lambda _: log.info(
                "Deepgram STT connected"))
            connection.on(EventType.MESSAGE, on_message)
            connection.on(EventType.CLOSE, lambda _: log.info(
                "Deepgram STT closed"))
            connection.on(EventType.ERROR, lambda e: log.error(
                f"Deepgram error: {e}"))

            connection.start_listening()

            # Record audio in chunks
            chunk_samples = int(SAMPLE_RATE * CHUNK_DURATION)
            silence_count = 0
            max_chunks = int(timeout / CHUNK_DURATION)

            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=chunk_samples
            ) as stream:

                for _ in range(max_chunks):
                    chunk, overflow = stream.read(chunk_samples)
                    if overflow:
                        log.warning("Audio buffer overflow")

                    # Convert to bytes and send to Deepgram
                    audio_bytes = chunk.tobytes()
                    connection.send(audio_bytes)

                    # Check for silence (simple RMS check)
                    rms = np.sqrt(np.mean(chunk.astype(np.float32) ** 2))
                    if rms < 500:  # Silence threshold
                        silence_count += 1
                        if silence_count * CHUNK_DURATION >= silence_threshold and final_received.is_set():
                            break
                    else:
                        silence_count = 0

            # Get final results
            connection.finish()
            final_received.wait(timeout=2.0)

    except Exception as e:
        log.error(f"Deepgram STT error: {e}")
        return f"[STT Error: {e}]"

    return transcript.strip()


# ════════════════════════════════════════════════════════════════════════════
#  TEXT-TO-SPEECH (TTS) — Replaces pyttsx3/Piper
# ════════════════════════════════════════════════════════════════════════════

def speak_deepgram(text: str) -> bool:
    """
    Synthesize speech with Deepgram Aura-2.
    Higher quality than pyttsx3, more reliable than Piper.

    Args:
        text: Text to speak (max 2000 chars per request)

    Returns:
        True if successful
    """
    if _dg_client is None:
        if not init_deepgram():
            return False

    try:
        # Deepgram TTS returns audio stream
        response = _dg_client.speak.v1.audio.generate(
            text=text[:2000],  # API limit
            model=TTS_MODEL,
            encoding="linear16",
            sample_rate=SAMPLE_RATE,
        )

        # Read audio data
        audio_data = response.stream.getvalue()
        audio_array = np.frombuffer(audio_data, dtype=np.int16)

        # Play audio
        sd.play(audio_array, samplerate=SAMPLE_RATE)
        sd.wait()

        return True

    except Exception as e:
        log.error(f"Deepgram TTS error: {e}")
        return False


def speak_deepgram_async(text: str) -> None:
    """Queue text for async TTS playback (non-blocking)."""
    _tts_queue.put(text)


def _tts_worker():
    """Background thread for async TTS."""
    while not _tts_stop_event.is_set():
        try:
            text = _tts_queue.get(timeout=0.5)
            if text:
                speak_deepgram(text)
        except queue.Empty:
            continue


def start_tts_worker():
    """Start background TTS thread."""
    global _tts_thread
    if _tts_thread is None or not _tts_thread.is_alive():
        _tts_stop_event.clear()
        _tts_thread = threading.Thread(target=_tts_worker, daemon=True)
        _tts_thread.start()
        log.info("Deepgram TTS worker started")


def stop_tts_worker():
    """Stop background TTS thread."""
    _tts_stop_event.set()
    if _tts_thread:
        _tts_thread.join(timeout=2.0)


# ════════════════════════════════════════════════════════════════════════════
#  DROP-IN REPLACEMENTS (Match existing NOVA interface)
# ════════════════════════════════════════════════════════════════════════════

def listen() -> str:
    """Drop-in replacement for NOVA's listen() function."""
    print("🎙️  Listening (Deepgram Nova-3)...")
    result = listen_deepgram()
    print(f"📝 Heard: {result}")
    return result


def speak(text: str) -> None:
    """Drop-in replacement for NOVA's speak() function."""
    print(f"\n🔊 NOVA: {text}\n")
    if not speak_deepgram(text):
        # Fallback to print if TTS fails
        print("[TTS failed - text only]")


# ════════════════════════════════════════════════════════════════════════════
#  TEST
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if init_deepgram():
        print("✅ Deepgram ready")
        print("Say something...")
        text = listen()
        print(f"Transcribed: {text}")
        if text:
            speak(f"You said: {text}")
    else:
        print("❌ Deepgram not available. Check DEEPGRAM_API_KEY in .env")
