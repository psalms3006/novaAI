from __future__ import annotations

"""NOVA configuration bridge.

Reads:
- nova_config.toml
- .env via dotenv

Exposes:
- get(dotpath, default)
- _find_config()
- _config / _config_path cache

Enhanced with validation, fallback defaults, and error logging.
"""

import logging
import os
from pathlib import Path
from typing import Any, Optional, Union, Dict, List

try:
    import tomllib as _tomllib
except ImportError:
    _tomllib = None  # type: ignore[misc]

try:
    from dotenv import load_dotenv as _load_dotenv
except ImportError:
    _load_dotenv = None  # type: ignore[misc]

_loaded_env = False
_logger = logging.getLogger(__name__)

_config_path: Optional[Path] = None
_config: Optional[dict] = None


def _ensure_env() -> None:
    global _loaded_env
    if not _loaded_env:
        if _load_dotenv is not None:
            _load_dotenv()
        _loaded_env = True


def _find_config() -> Path:
    return Path("nova_config.toml")


def _default_config() -> dict:
    """Return a dictionary with default configuration values."""
    return {
        "nova": {
            "name": "NOVA",
            "voice": "Aoede",
            "max_retries": 3,
            "history_turns": 6,
        },
        "model": {
            "live_model": "models/gemini-3.1-flash-live-preview",
            "vision_model": "gemini-flash-latest",
            "embed_model": "./nova_embedder",
            "whisper_size": "tiny",
        },
        "memory": {
            "top_k": 5,
            "min_score": 0.30,
            "max_combined_length": 2000,
            "extract_every_n": 5,
        },
        "rate_limit": {
            "min_gap_secs": 6.0,
            "backoff_max_secs": 1800,
        },
        "audio": {
            "send_sample_rate": 16000,
            "receive_sample_rate": 24000,
            "chunk_size": 1024,
        },
        "tts": {
            "rate": 165,
            "volume": 0.95,
        },
        "heartbeat": {
            "interval": 60,
            "min_gap": 300,
            "quiet_start": 22,
            "quiet_end": 7,
        },
        "safety": {
            "consequential_tools": [
                "self_editor",
                "send_message",
                "autostart",
                "game_updater",
            ],
            "safe_file_actions": ["list", "read", "find", "info"],
            "audit_max_lines": 500,
        },
        "server": {
            "phone_port": 5050,
            "ui_port": 8080,
        },
        "offline": {
            "models": ["tinyllama", "llama3.2", "phi3", "mistral"],
            "timeouts": {
                "tinyllama": 15,
                "llama3_2": 30,
                "phi3": 30,
                "mistral": 45,
            },
        },
"identity": {
            "assistant_name": "Nova",
            "company": "Omniel",
            "aliases": [],
        },
        "comms": {
            "enabled": True,
            "phone_number": "",
            "phone_verified": False,
            "timezone": "UTC",
            "quiet_hours_enabled": True,
            "quiet_start": 22,
            "quiet_end": 7,
            "voice_provider": "bland",
            "bland_voice": "june",
            "max_call_duration": 10,
            "max_calls_per_hour": 3,
            "max_calls_per_day": 10,
            "retry_max_attempts": 3,
            "retry_base_interval_seconds": 300,
            "retry_max_interval_seconds": 3600,
            "retry_exponential_backoff": True,
            "retry_jitter": True,
            "escalation_fallback_channels": ["desktop"],
            "require_confirmation_for": [
                "send_message",
                "autostart",
                "game_updater",
                "self_editor",
            ],
            "allowed_auto_actions": [
                "web_search",
                "file_controller:read",
                "file_controller:list",
                "recall",
                "nova_memory:read",
            ],
            "webhook_base_url": "",
        },
    }


def _validate_and_defaults(user_config: dict) -> dict:
    """Validate user config against defaults, applying defaults for missing values.
    Also, perform basic type checking and log warnings for invalid values.
    """
    defaults = _default_config()
    # We'll merge user_config into defaults, but we want to keep the structure of defaults
    # and only override with user values if they are of the correct type.
    # For simplicity, we do a deep update and then validate types.
    merged = _deep_update(defaults, user_config or {})
    # Now validate each leaf node against the default's type.
    _validate_types(merged, defaults, path="")
    return merged


def _deep_update(defaults: dict, user: dict) -> dict:
    """Recursively update defaults with user values, preserving dict structure."""
    result = {}
    for key, value in defaults.items():
        if key in user:
            if isinstance(value, dict) and isinstance(user[key], dict):
                result[key] = _deep_update(value, user[key])
            else:
                # Override with user value (we'll validate type later)
                result[key] = user[key]
        else:
            result[key] = value
    # Also, if user has extra keys not in defaults, we keep them? 
    # For safety, we ignore extra keys to avoid unexpected configuration.
    for key in user:
        if key not in defaults:
            _logger.warning("Ignoring unknown configuration key: %s", key)
    return result


def _validate_types(config: dict, defaults: dict, path: str) -> None:
    """Recursively validate that config values match the type of defaults.
    Log warnings for mismatches but do not raise exceptions (to avoid breaking the app).
    """
    for key, default_value in defaults.items():
        full_path = f"{path}.{key}" if path else key
        if key not in config:
            # This should not happen because of _deep_update, but just in case.
            _logger.warning("Missing configuration key: %s", full_path)
            continue
        value = config[key]
        if isinstance(default_value, dict):
            if not isinstance(value, dict):
                _logger.warning(
                    "Configuration key %s expects a dict, got %s. Using default.",
                    full_path,
                    type(value).__name__,
                )
                config[key] = default_value
            else:
                _validate_types(value, default_value, full_path)
        else:
            # Check type
            expected_type = type(default_value)
            if not isinstance(value, expected_type):
                # Try to be lenient for numeric types: int vs float, etc.
                if expected_type == int and isinstance(value, (float, int)):
                    # Allow float that is integer
                    if isinstance(value, float) and not value.is_integer():
                        _logger.warning(
                            "Configuration key %s expects an integer, got %s. Using default.",
                            full_path,
                            value,
                        )
                        config[key] = default_value
                    else:
                        config[key] = int(value)
                elif expected_type == float and isinstance(value, (float, int)):
                    config[key] = float(value)
                elif expected_type == bool and isinstance(value, bool):
                    pass
                else:
                    _logger.warning(
                        "Configuration key %s expects %s, got %s. Using default.",
                        full_path,
                        expected_type.__name__,
                        type(value).__name__,
                    )
                    config[key] = default_value


def _load_config() -> dict:
    """Load configuration from nova_config.toml, apply defaults, validate, and log errors."""
    _ensure_env()
    global _config
    if _config is not None:
        return _config

    path = _find_config()
    raw_config: dict = {}
    if path.exists():
        if _tomllib is not None:
            try:
                raw_config = _tomllib.loads(path.read_text(encoding="utf-8"))
                _logger.info("Loaded configuration from %s", path)
            except Exception as exc:
                _logger.error(
                    "Failed to parse nova_config.toml: %s. Using defaults.", exc
                )
        else:
            _logger.error(
                "tomllib not available. Install tomli or use Python 3.11+. Using defaults."
            )
    else:
        _logger.warning("Configuration file %s not found. Using defaults.", path)

    # Apply defaults and validate
    _config = _validate_and_defaults(raw_config)
    return _config


def get(dotpath: str, default: Any = None) -> Any:
    """Get a configuration value using dot notation (e.g., 'nova.name')."""
    config = _load_config()
    keys = dotpath.split(".")
    cur: Any = config
    for key in keys:
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return default
    return cur if cur is not None else default


# For backward compatibility, expose the internal functions if needed.
__all__ = ["get"]
