"""
NOVA Voice Module
-----------------
Primary  : ElevenLabs TTS (human-quality, JARVIS-like)
Fallback : pyttsx3 (offline, robotic but functional)

Setup:
  1. pip install elevenlabs python-dotenv pyttsx3
  2. sudo apt install mpg123 -y
  3. Create a .env file in your project root:
       ELEVEN_API_KEY=your_api_key_here
       ELEVEN_VOICE_ID=your_voice_id_here
"""

import os
import time
import socket
import logging
import tempfile
import pyttsx3
import requests
from dotenv import load_dotenv

# ── Load environment variables ──────────────────────────────────────────────
load_dotenv()

ELEVEN_API_KEY = os.getenv("ELEVEN_API_KEY")
ELEVEN_VOICE_ID = os.getenv("ELEVEN_VOICE_ID")

# ── Logging setup ────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="[NOVA Voice] %(message)s")
log = logging.getLogger(__name__)

# ── pyttsx3 engine (initialised once, reused) ────────────────────────────────
_local_engine = None


def _get_local_engine() -> pyttsx3.Engine:
    """Initialise pyttsx3 once and cache it."""
    global _local_engine
    if _local_engine is None:
        _local_engine = pyttsx3.init()
        _local_engine.setProperty("rate", 175)     # words per minute
        _local_engine.setProperty("volume", 0.95)  # 0.0 – 1.0

        # Pick the most natural-sounding voice available
        voices = _local_engine.getProperty("voices")
        if voices is None:
            voices = []
        elif not isinstance(voices, (list, tuple)):
            voices = [voices]

        for v in voices:
            # Prefer a male English voice if present
            voice_name = getattr(v, "name", "") or ""
            voice_id = getattr(v, "id", "") or ""
            if "english" in voice_name.lower() or "en" in voice_id.lower():
                _local_engine.setProperty("voice", voice_id)
                break

    return _local_engine


# ── Internet connectivity check ──────────────────────────────────────────────
def _is_online(host: str = "8.8.8.8", port: int = 53, timeout: float = 2.0) -> bool:
    """Return True if an outbound connection can be made (fast DNS probe)."""
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except OSError:
        return False


# ── ElevenLabs TTS ───────────────────────────────────────────────────────────
def _speak_elevenlabs(text: str) -> bool:
    """
    Send text to ElevenLabs API, save the audio to a temp file, and play it.
    Returns True on success, False on any error.
    """
    if not ELEVEN_API_KEY or not ELEVEN_VOICE_ID:
        log.warning("ElevenLabs credentials missing. Check your .env file.")
        return False

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}"

    headers = {
        "xi-api-key": ELEVEN_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }

    payload = {
        "text": text,
        "model_id": "eleven_turbo_v2",   # fastest + cheapest — ideal for Pi
        "voice_settings": {
            "stability": 0.75,           # consistent, calm delivery
            "similarity_boost": 0.85,    # stays true to chosen voice
            "style": 0.30,               # subtle expressiveness
            "use_speaker_boost": True,   # crisper audio on small speakers
        },
    }

    try:
        log.info("Sending to ElevenLabs...")
        response = requests.post(
            url, json=payload, headers=headers, timeout=10)
        response.raise_for_status()

        # Write audio to a temp file and play with mpg123
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(response.content)
            tmp_path = tmp.name

        log.info("Playing ElevenLabs audio...")
        exit_code = os.system(f"mpg123 -q {tmp_path}")  # -q = quiet mode
        os.unlink(tmp_path)  # clean up temp file

        return exit_code == 0

    except requests.exceptions.Timeout:
        log.error("ElevenLabs request timed out.")
    except requests.exceptions.HTTPError as e:
        if e.response is not None:
            log.error(
                f"ElevenLabs HTTP error: {e.response.status_code} — {e.response.text}")
        else:
            log.error("ElevenLabs HTTP error: no response available.")
    except Exception as e:
        log.error(f"ElevenLabs unexpected error: {e}")

    return False


# ── Local TTS fallback ───────────────────────────────────────────────────────
def _speak_local(text: str) -> None:
    """Speak using pyttsx3 — fully offline, no API needed."""
    log.info("Using local TTS (offline fallback)...")
    engine = _get_local_engine()
    engine.say(text)
    engine.runAndWait()


# ── Main public function ─────────────────────────────────────────────────────
def speak(text: str, force_local: bool = False) -> None:
    """
    Speak the given text using the best available TTS engine.

    Args:
        text        : The string NOVA should say.
        force_local : If True, skip ElevenLabs entirely (useful for testing).

    Behaviour:
        1. If online and credentials exist → ElevenLabs (JARVIS quality)
        2. If ElevenLabs fails or offline   → pyttsx3 (local fallback)
    """
    if not text or not text.strip():
        log.warning("speak() called with empty text. Skipping.")
        return

    text = text.strip()

    if force_local:
        _speak_local(text)
        return

    if _is_online():
        success = _speak_elevenlabs(text)
        if success:
            return
        log.warning("ElevenLabs failed. Falling back to local TTS.")
    else:
        log.info("Offline detected. Using local TTS.")

    _speak_local(text)


# ── Quick test ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Testing NOVA voice module...\n")

    test_lines = [
        "Initialising NOVA. All systems are online.",
        "Good evening, sir. How may I assist you today?",
        "Processing your request. Please stand by.",
    ]

    for line in test_lines:
        print(f"Speaking: {line}")
        speak(line)
        time.sleep(0.5)

    print("\nVoice test complete.")
