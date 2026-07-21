"""
gemini_compat.py — Gemini SDK Compatibility Layer
═══════════════════════════════════════════════════════════════════════════════
Isolates ALL google-genai SDK specifics behind a stable internal API.

WHY:
  The google-genai SDK changes field names, restructures config objects,
  and adds/removes fields between minor versions.  Previous NOVA versions
  broke with ValidationError when `voice_config` moved from the top-level
  LiveConnectConfig into a nested `speech_config.voice_config` path.

  This module is the SINGLE PLACE that knows about SDK schema details.
  Every other NOVA file calls build_live_config() and gets back a valid
  config object — regardless of SDK version.

DESIGN:
  - Probes the installed SDK's LiveConnectConfig at import time
  - Builds config using ONLY the fields the current SDK accepts
  - Falls back gracefully if a field is missing (logs warning, continues)
  - Never raises ValidationError to the caller

USAGE:
  from gemini_compat import build_live_config, SDK_INFO

  config = build_live_config(
      voice_name="Charon",
      system_instruction="You are NOVA...",
      tool_declarations=[...],
  )
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Dict, List, Optional

log = logging.getLogger("NOVA.gemini_compat")

# ---------------------------------------------------------------------------
# SDK Detection
# ---------------------------------------------------------------------------

SDK_INFO: Dict[str, Any] = {"version": "unknown", "has_live": False}

try:
    import google.genai as _genai_mod
    SDK_INFO["version"] = getattr(_genai_mod, "__version__", "unknown")

    from google.genai import types as _gt

    # Client.aio is a property, not a class attr — probe it gently
    _has_live = False
    try:
        _probe_client = _genai_mod.Client(api_key="probe")
        _aio = getattr(_probe_client, "aio", None)
        if _aio:
            _live = getattr(_aio, "live", None)
            _has_live = _live is not None and hasattr(_live, "connect")
        del _probe_client
    except Exception:
        # Even if probe fails, the live API typically exists in modern SDKs
        _has_live = hasattr(_gt, "LiveConnectConfig")
    SDK_INFO["has_live"] = _has_live
    SDK_INFO["has_types"] = True
except ImportError:
    _genai_mod = None
    _gt = None
    log.warning("google-genai SDK not installed — Gemini Live unavailable")


def _probe_lcc_fields() -> set:
    """Return the set of field names accepted by LiveConnectConfig."""
    if _gt is None:
        return set()
    cls = getattr(_gt, "LiveConnectConfig", None)
    if cls is None:
        return set()
    if hasattr(cls, "model_fields"):
        return set(cls.model_fields.keys())
    return set()


# Cache the probe result — computed once at import time
_LCC_FIELDS: set = _probe_lcc_fields()


def _field_supported(field_name: str) -> bool:
    """Check if a field name is accepted by the installed SDK's LiveConnectConfig."""
    return field_name in _LCC_FIELDS


# ---------------------------------------------------------------------------
# Config Builder — the main public API
# ---------------------------------------------------------------------------


def build_live_config(
    *,
    voice_name: str = "Charon",
    system_instruction: str = "",
    tool_declarations: Optional[List[Dict[str, Any]]] = None,
    response_modalities: Optional[List[str]] = None,
    enable_input_transcription: bool = True,
    enable_output_transcription: bool = True,
    session_resumption: bool = False,
) -> Any:
    """Build a LiveConnectConfig that works with the INSTALLED SDK version.

    This is the ONLY place in NOVA that constructs LiveConnectConfig.
    Every other module calls this function.

    Args:
        voice_name: Prebuilt voice name (e.g. "Charon", "Puck", "Fenrir").
        system_instruction: System prompt text.
        tool_declarations: List of function declaration dicts for tool calling.
        response_modalities: List of modality strings, defaults to ["AUDIO"].
        enable_input_transcription: Enable transcription of user audio input.
        enable_output_transcription: Enable transcription of model audio output.
        session_resumption: Enable session resumption support.

    Returns:
        A valid LiveConnectConfig object, or None if SDK is unavailable.

    Raises:
        No exceptions — logs warnings and returns None on any failure.
    """
    if _gt is None or not SDK_INFO["has_live"]:
        log.error("Cannot build config: google-genai SDK not available")
        return None

    # Default modalities
    if response_modalities is None:
        response_modalities = ["AUDIO"]

    # Build the config kwargs dict — only include fields the SDK accepts
    kwargs: Dict[str, Any] = {}

    # --- response_modalities (core, always present) ---
    try:
        Modality = _gt.Modality
        modalities = [Modality.AUDIO] if "AUDIO" in response_modalities else []
        if "TEXT" in response_modalities:
            modalities.append(Modality.TEXT)
        kwargs["response_modalities"] = modalities
    except Exception as e:
        log.warning(f"Failed to set response_modalities: {e}")

    # --- system_instruction ---
    if system_instruction:
        kwargs["system_instruction"] = system_instruction

    # --- tools ---
    if tool_declarations:
        kwargs["tools"] = [{"function_declarations": tool_declarations}]

    # --- speech_config (voice) — THIS IS THE KEY DIFFERENCE ---
    # SDK v2.10+ nests voice under speech_config.voice_config.prebuilt_voice_config
    # Older SDKs (if any exist) may have had voice_config at top level
    voice_set = False

    # Path A: speech_config (SDK v2.10+)
    if _field_supported("speech_config"):
        try:
            SpeechConfig = _gt.SpeechConfig
            VoiceConfig = _gt.VoiceConfig
            PrebuiltVoiceConfig = _gt.PrebuiltVoiceConfig

            speech_cfg = SpeechConfig(
                voice_config=VoiceConfig(
                    prebuilt_voice_config=PrebuiltVoiceConfig(voice_name=voice_name)
                )
            )
            kwargs["speech_config"] = speech_cfg
            voice_set = True
        except Exception as e:
            log.warning(f"Failed to build speech_config (voice={voice_name}): {e}")
            # Fall through to Path B

    # Path B: voice_config at top level (older SDKs — pre-2.10)
    if not voice_set and _field_supported("voice_config"):
        try:
            VoiceConfig = _gt.VoiceConfig
            PrebuiltVoiceConfig = _gt.PrebuiltVoiceConfig
            kwargs["voice_config"] = VoiceConfig(
                prebuilt_voice_config=PrebuiltVoiceConfig(voice_name=voice_name)
            )
            voice_set = True
            log.info("Using legacy voice_config path (SDK may be outdated)")
        except Exception as e:
            log.warning(f"Failed to build legacy voice_config: {e}")

    if not voice_set:
        log.warning("Could not configure voice — NOVA will use default voice")

    # --- input/output audio transcription ---
    if _field_supported("input_audio_transcription") and enable_input_transcription:
        try:
            kwargs["input_audio_transcription"] = _gt.AudioTranscriptionConfig()
        except Exception as e:
            log.warning(f"Failed to set input_audio_transcription: {e}")

    if _field_supported("output_audio_transcription") and enable_output_transcription:
        try:
            kwargs["output_audio_transcription"] = _gt.AudioTranscriptionConfig()
        except Exception as e:
            log.warning(f"Failed to set output_audio_transcription: {e}")

    # --- session_resumption ---
    if _field_supported("session_resumption") and session_resumption:
        try:
            kwargs["session_resumption"] = _gt.SessionResumptionConfig()
        except Exception as e:
            log.warning(f"Failed to set session_resumption: {e}")

    # --- Construct the final config ---
    try:
        config = _gt.LiveConnectConfig(**kwargs)
        log.debug(f"LiveConnectConfig built successfully ({len(kwargs)} fields)")
        return config
    except Exception as e:
        log.error(f"LiveConnectConfig construction FAILED: {e}")
        log.error(f"  Attempted kwargs: {list(kwargs.keys())}")
        log.error(f"  SDK version: {SDK_INFO['version']}")
        log.error(f"  Supported fields: {sorted(_LCC_FIELDS)}")
        # Last resort: build minimal config with only response_modalities
        try:
            minimal = _gt.LiveConnectConfig(
                response_modalities=[_gt.Modality.AUDIO]
            )
            log.warning("Fell back to minimal config (voice disabled)")
            return minimal
        except Exception as e2:
            log.critical(f"Minimal config also failed: {e2}")
            return None


# ---------------------------------------------------------------------------
# Convenience: get the types module for callers that need Modality, etc.
# ---------------------------------------------------------------------------

def get_types():
    """Return the google.genai.types module, or None."""
    return _gt


# ---------------------------------------------------------------------------
# Validation helper — call at startup to verify SDK compatibility
# ---------------------------------------------------------------------------

def validate_sdk() -> Dict[str, Any]:
    """Run a full compatibility check. Call once at NOVA startup.

    Returns a dict with:
        compatible: bool — can NOVA use Gemini Live?
        version: str — SDK version string
        config_ok: bool — did build_live_config() produce a valid config?
        issues: list[str] — any problems found
    """
    issues: List[str] = []
    compatible = True

    if not SDK_INFO["has_types"]:
        issues.append("google-genai SDK not importable")
        compatible = False
        return {"compatible": False, "version": SDK_INFO["version"],
                "config_ok": False, "issues": issues}

    # Check required types exist
    required = ["LiveConnectConfig", "Modality", "SpeechConfig",
                "VoiceConfig", "PrebuiltVoiceConfig", "AudioTranscriptionConfig"]
    for name in required:
        if not hasattr(_gt, name):
            issues.append(f"Missing type: {gt}.{name}")
            compatible = False

    if not SDK_INFO["has_live"]:
        issues.append("Client.aio.live.connect not found — Live API unavailable")
        compatible = False

    # Try building a config
    config_ok = False
    if compatible:
        test_config = build_live_config(
            voice_name="Charon",
            system_instruction="test",
            tool_declarations=[{"name": "test", "description": "test"}],
        )
        config_ok = test_config is not None
        if not config_ok:
            issues.append("build_live_config() returned None")
            compatible = False

    return {
        "compatible": compatible,
        "version": SDK_INFO["version"],
        "config_ok": config_ok,
        "issues": issues,
    }


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    print("=== NOVA Gemini SDK Compatibility Layer — Self-Test ===\n")

    print(f"SDK version: {SDK_INFO['version']}")
    print(f"Has types: {SDK_INFO.get('has_types', False)}")
    print(f"Has live: {SDK_INFO['has_live']}")
    print(f"LiveConnectConfig fields ({len(_LCC_FIELDS)}): {sorted(_LCC_FIELDS)}")
    print()

    result = validate_sdk()
    print(f"Compatible: {result['compatible']}")
    print(f"Config OK: {result['config_ok']}")
    if result["issues"]:
        print(f"Issues: {result['issues']}")
    print()

    if result["config_ok"]:
        config = build_live_config(
            voice_name="Charon",
            system_instruction="You are a helpful assistant.",
            tool_declarations=[{"name": "test_tool", "description": "A test tool",
                                "parameters": {"type": "object", "properties": {}}}],
        )
        if config:
            dump = config.model_dump(by_alias=True)
            print("Built config (camelCase keys):")
            for k, v in dump.items():
                if v is not None and v != [] and v != {}:
                    print(f"  {k}: {v}")