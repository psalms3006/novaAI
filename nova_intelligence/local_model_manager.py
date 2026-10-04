"""nova_intelligence.local_model_manager — Ollama model lifecycle management.

Handles model discovery, download, removal, capability metadata, and health
checking. Designed so that NOVA can auto-setup the local runtime without
requiring the user to be technical.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from .ollama_provider import OLLAMA_DEFAULT_URL, OllamaProvider

log = logging.getLogger(__name__)

# ── Model capability metadata ─────────────────────────────────────────────────

KNOWN_MODELS: List[Dict[str, Any]] = [
    {
        "id": "qwen2.5:3b",
        "name": "NOVA Local Core",
        "description": "Compact model optimized for tool calling and instruction following.",
        "size_gb": 0.6,
        "context_length": 32768,
        "tool_calling": True,
        "vision": False,
        "reasoning": "good",
        "recommended_ram_gb": 2.0,
        "speed": "fast",
        "strengths": ["Conversation", "Tool calling", "Instruction following", "File operations"],
        "limitations": ["Complex reasoning", "Current events", "Cloud APIs"],
        "recommended": True,
    },
    {
        "id": "llama3.2:3b",
        "name": "Llama 3.2 3B",
        "description": "Meta's lightweight Llama with solid all-around performance.",
        "size_gb": 2.0,
        "context_length": 131072,
        "tool_calling": True,
        "vision": False,
        "reasoning": "good",
        "recommended_ram_gb": 4.0,
        "speed": "fast",
        "strengths": ["Conversation", "Tool calling", "Long context"],
        "limitations": ["Complex reasoning", "Current events"],
        "recommended": False,
    },
    {
        "id": "phi3:mini",
        "name": "Phi-3 Mini",
        "description": "Microsoft's small but capable model.",
        "size_gb": 2.2,
        "context_length": 128000,
        "tool_calling": True,
        "vision": False,
        "reasoning": "good",
        "recommended_ram_gb": 4.0,
        "speed": "fast",
        "strengths": ["Conversation", "Reasoning", "Tool calling"],
        "limitations": ["Current events", "Cloud APIs"],
        "recommended": False,
    },
    {
        "id": "mistral:latest",
        "name": "Mistral",
        "description": "Strong reasoning and code generation.",
        "size_gb": 4.1,
        "context_length": 32768,
        "tool_calling": True,
        "vision": False,
        "reasoning": "very good",
        "recommended_ram_gb": 8.0,
        "speed": "moderate",
        "strengths": ["Reasoning", "Code", "Tool calling"],
        "limitations": ["Larger download", "Slower on low RAM"],
        "recommended": False,
    },
    {
        "id": "tinyllama",
        "name": "TinyLlama",
        "description": "Ultra-lightweight fallback. Basic conversation only.",
        "size_gb": 0.6,
        "context_length": 2048,
        "tool_calling": False,
        "vision": False,
        "reasoning": "basic",
        "recommended_ram_gb": 2.0,
        "speed": "very fast",
        "strengths": ["Very small", "Fast", "Basic conversation"],
        "limitations": ["No tool calling", "Short context", "Basic reasoning"],
        "recommended": False,
    },
    {
        "id": "gemma2:2b",
        "name": "Gemma 2 2B",
        "description": "Google's compact model with good instruction following.",
        "size_gb": 1.6,
        "context_length": 8192,
        "tool_calling": True,
        "vision": False,
        "reasoning": "good",
        "recommended_ram_gb": 4.0,
        "speed": "fast",
        "strengths": ["Instruction following", "Tool calling", "Compact"],
        "limitations": ["Shorter context", "Current events"],
        "recommended": False,
    },
]

DEFAULT_MODEL_ID = "qwen2.5:3b"


class LocalModelManager:
    """Manages Ollama model lifecycle and metadata."""

    def __init__(self, base_url: str = OLLAMA_DEFAULT_URL):
        self._base_url = base_url
        self._provider: Optional[OllamaProvider] = None
        self._installed_cache: Optional[List[Dict]] = None
        self._cache_time = 0.0
        self._lock = threading.Lock()

    def _get_provider(self) -> OllamaProvider:
        if self._provider is None:
            model = self._load_current_model()
            self._provider = OllamaProvider(base_url=self._base_url, model=model)
        return self._provider

    def _configured_model(self) -> str:
        try:
            from desk.settings import get as _get_setting
            return _get_setting("local_model", DEFAULT_MODEL_ID) or DEFAULT_MODEL_ID
        except Exception:
            return DEFAULT_MODEL_ID

    def _load_current_model(self) -> str:
        """Resolve the local model to use, preferring one that is installed.

        The configured name used to be returned unchecked. If it was not
        installed — the default qwen2.5:3b on a machine that has something else,
        or a model the user removed — OllamaProvider.is_available() returned
        False and the router quietly dropped the local provider entirely. NOVA
        then reported "no intelligence provider available" while perfectly good
        models sat installed on disk.

        The user's explicit choice always wins when it is actually present.
        """
        configured = self._configured_model()
        try:
            installed = [
                m.get("name", "")
                for m in OllamaProvider(base_url=self._base_url).list_models()
            ]
        except Exception:
            return configured
        if not installed:
            return configured
        if any(configured in name for name in installed):
            return configured

        best = self._best_installed(installed)
        if best:
            log.warning(
                "[LOCAL] configured model %r is not installed; using %r instead "
                "(installed: %s)", configured, best, ", ".join(installed),
            )
            return best
        return configured

    @staticmethod
    def _best_installed(installed: List[str]) -> Optional[str]:
        """Pick the most capable installed model.

        NOVA's core loop is tool calling, so a model that cannot call tools
        (tinyllama) is a last resort no matter how fast it is.
        """
        _REASONING_RANK = {"very good": 3, "good": 2, "basic": 1}
        meta_by_id = {m["id"]: m for m in KNOWN_MODELS}

        def score(name: str) -> tuple:
            meta = None
            for mid, m in meta_by_id.items():
                if mid.split(":")[0] in name:
                    meta = m
                    break
            if meta is None:
                # Unknown model: assume it calls tools, rank mid.
                return (1, 2, 0.0)
            return (
                1 if meta.get("tool_calling") else 0,
                _REASONING_RANK.get(meta.get("reasoning", "basic"), 1),
                float(meta.get("size_gb", 0.0)),
            )

        ranked = sorted(installed, key=score, reverse=True)
        return ranked[0] if ranked else None

    @property
    def current_model(self) -> str:
        return self._get_provider().model

    @property
    def configured_model(self) -> str:
        """The model NOVA is set to use, without checking it is installed.

        `current_model` asks Ollama what it actually has, which is the right
        answer and costs a round trip to a service that may not be running —
        measured at 4.1 s on this machine, spent during startup, before the
        microphone was open. When the cloud is available Ollama is only ever
        a fallback and is deliberately left stopped, so that check belongs at
        the moment the fallback is first wanted, not at launch.
        """
        return self._configured_model()

    def get_status(self) -> dict:
        """Full status of local model infrastructure."""
        provider = self._get_provider()
        running = provider.is_running()
        available = provider.is_available()
        installed = self.list_installed()

        return {
            "runtime_available": running,
            "current_model": provider.model,
            "current_model_installed": available,
            "installed_count": len(installed),
            "installed_models": installed,
            "health": provider.health.snapshot(),
            "base_url": self._base_url,
        }

    def list_installed(self) -> List[Dict[str, Any]]:
        """List models actually installed in Ollama."""
        now = __import__("time").time()
        with self._lock:
            if self._installed_cache and (now - self._cache_time) < 30:
                return self._installed_cache

        provider = self._get_provider()
        models = provider.list_models()
        with self._lock:
            self._installed_cache = models
            self._cache_time = now
        return models

    def list_available(self) -> List[Dict[str, Any]]:
        """List all known models with metadata and install status."""
        installed = {m["name"] for m in self.list_installed()}
        result = []
        for model in KNOWN_MODELS:
            entry = dict(model)
            entry["installed"] = any(
                model["id"] in name for name in installed
            )
            result.append(entry)
        return result

    def get_recommended(self) -> Optional[Dict[str, Any]]:
        """Get the recommended model for first-run setup."""
        for m in KNOWN_MODELS:
            if m.get("recommended"):
                return dict(m)
        return KNOWN_MODELS[0] if KNOWN_MODELS else None

    def set_current_model(self, model_id: str) -> bool:
        """Set the active model. Returns True if successful."""
        provider = self._get_provider()
        provider.model = model_id
        try:
            from desk.settings import set_many as _set_many
            _set_many({"local_model": model_id})
        except Exception:
            pass
        log.info("[MODEL] current model set to %s", model_id)
        return True

    def download_model(self, model_id: str, progress_fn: Optional[callable] = None) -> bool:
        """Download a model. Blocks until complete."""
        provider = self._get_provider()
        log.info("[MODEL] downloading %s ...", model_id)
        ok = provider.download_model(model_id)
        if ok:
            with self._lock:
                self._installed_cache = None
        return ok

    def remove_model(self, model_id: str) -> bool:
        provider = self._get_provider()
        ok = provider.remove_model(model_id)
        if ok:
            with self._lock:
                self._installed_cache = None
        return ok

    def test_model(self, model_id: Optional[str] = None) -> dict:
        """Test a model's responsiveness."""
        provider = self._get_provider()
        if model_id and model_id != provider.model:
            old = provider.model
            provider.model = model_id
            result = provider.test_model()
            provider.model = old
            return result
        return provider.test_model()

    def get_model_metadata(self, model_id: str) -> Optional[Dict[str, Any]]:
        """Get metadata for a known model."""
        for m in KNOWN_MODELS:
            if m["id"] == model_id:
                return dict(m)
        return None


# ── Module singleton ──────────────────────────────────────────────────────────
_manager: Optional[LocalModelManager] = None


def get_model_manager() -> LocalModelManager:
    global _manager
    if _manager is None:
        _manager = LocalModelManager()
    return _manager
