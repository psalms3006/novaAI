"""nova_intelligence.voice_provider — Minimal voice provider abstraction.

Provides a thin interface so the rest of NOVA can switch between
Gemini Live (online) and local STT/TTS (offline) without rewriting
the voice system. The actual voice logic stays in desk/voice.py and
desk/live_session.py — this module just routes between them.
"""
from __future__ import annotations

import logging
from enum import Enum, auto
from typing import Optional

log = logging.getLogger(__name__)


class VoiceMode(Enum):
    """Available voice modes."""
    GEMINI_LIVE = auto()      # Online: Gemini native audio
    LOCAL_STT_TTS = auto()    # Offline: faster-whisper + local TTS
    PUSH_TO_TALK = auto()     # Fallback: manual PTT
    DISABLED = auto()


class VoiceProvider:
    """Thin abstraction for voice mode selection.
    
    This does NOT implement voice logic — it delegates to the existing
    desk/voice.py and desk/live_session.py. It just knows which mode
    is available and provides a unified interface for the bridge.
    """

    def __init__(self):
        self._mode = VoiceMode.DISABLED
        self._gemini_live_available = False
        self._local_stt_available = False
        self._local_tts_available = False

    @property
    def mode(self) -> VoiceMode:
        return self._mode

    @property
    def mode_name(self) -> str:
        return self._mode.name

    def detect(self) -> VoiceMode:
        """Detect which voice mode is available. Returns best available mode."""
        # Check Gemini Live
        try:
            from google import genai
            import os
            self._gemini_live_available = bool(os.environ.get("GEMINI_API_KEY"))
        except ImportError:
            self._gemini_live_available = False

        # Check local STT (faster-whisper or deepgram)
        try:
            import desk.voice as voice
            self._local_stt_available = voice.transcription_available()
        except Exception:
            self._local_stt_available = False

        # Check local TTS
        try:
            import desk.voice as voice
            self._local_tts_available = voice.audio_available()
        except Exception:
            self._local_tts_available = False

        # Select best available mode
        if self._gemini_live_available:
            self._mode = VoiceMode.GEMINI_LIVE
        elif self._local_stt_available:
            self._mode = VoiceMode.LOCAL_STT_TTS
        else:
            self._mode = VoiceMode.DISABLED

        log.info("[VOICE] Detected mode: %s (live=%s, stt=%s, tts=%s)",
                 self._mode.name, self._gemini_live_available,
                 self._local_stt_available, self._local_tts_available)
        return self._mode

    def status(self) -> dict:
        """Return voice provider status for the bridge."""
        return {
            "mode": self._mode.name,
            "gemini_live": self._gemini_live_available,
            "local_stt": self._local_stt_available,
            "local_tts": self._local_tts_available,
            "available": self._mode != VoiceMode.DISABLED,
        }
