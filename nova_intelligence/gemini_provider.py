"""nova_intelligence.gemini_provider — Gemini provider wrapper.

Wraps the existing google-genai SDK behind the IntelligenceProvider protocol.
This allows the router to treat Gemini and Ollama identically.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Iterator, List, Optional

from .provider import GenerateResult, ProviderCapability, ProviderHealth

log = logging.getLogger(__name__)


class GeminiProvider:
    """Google Gemini intelligence provider."""

    def __init__(
        self,
        model: str = "gemini-flash-latest",
        api_key: Optional[str] = None,
    ):
        self._model = model
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self._health = ProviderHealth()
        self._capabilities = (
            ProviderCapability.CHAT
            | ProviderCapability.TOOL_CALLING
            | ProviderCapability.VISION
            | ProviderCapability.STREAMING
            | ProviderCapability.SYSTEM_INSTRUCTION
        )
        self._client = None

    @property
    def name(self) -> str:
        return "gemini"

    @property
    def capabilities(self) -> ProviderCapability:
        return self._capabilities

    @property
    def health(self) -> ProviderHealth:
        return self._health

    def is_available(self) -> bool:
        if not self._api_key:
            return False
        try:
            from google import genai
            return True
        except ImportError:
            return False

    def _get_client(self):
        if self._client is None:
            from google import genai
            from google.genai import types as gtypes
            self._client = genai.Client(
                api_key=self._api_key,
                http_options=gtypes.HttpOptions(timeout=60000),
            )
        return self._client

    def complete(
        self,
        messages: List[Dict[str, Any]],
        system: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> GenerateResult:
        t0 = time.time()
        try:
            from google.genai import types as gtypes

            client = self._get_client()

            # Build contents
            contents = []
            for m in messages:
                role = m.get("role", "user")
                content = m.get("content", "")
                if role == "system":
                    system = content  # will be passed as config
                    continue
                contents.append(gtypes.Content(
                    role="user" if role == "user" else "model",
                    parts=[gtypes.Part(text=content)],
                ))

            if not contents:
                return GenerateResult(error="No messages to process", provider="gemini")

            config = gtypes.GenerateContentConfig(
                temperature=temperature,
                max_output_tokens=max_tokens,
            )
            if system:
                config.system_instruction = system
            if tools:
                config.tools = tools

            response = client.models.generate_content(
                model=self._model,
                contents=contents,
                config=config,
            )

            latency = (time.time() - t0) * 1000
            text = response.text or ""

            # Parse tool calls
            tool_calls = []
            if response.candidates:
                for part in response.candidates[0].content.parts:
                    if part.function_call:
                        tc = part.function_call
                        tool_calls.append({
                            "name": tc.name,
                            "args": dict(tc.args) if tc.args else {},
                        })

            self._health.record_success(latency)
            return GenerateResult(
                text=text,
                tool_calls=tool_calls,
                provider="gemini",
                model=self._model,
                latency_ms=latency,
                raw=response,
            )
        except Exception as e:
            latency = (time.time() - t0) * 1000
            self._health.record_failure(str(e))
            log.warning("[GEMINI] complete error: %s", e)
            return GenerateResult(
                error=str(e),
                provider="gemini",
                model=self._model,
                latency_ms=latency,
            )

    def stream(
        self,
        messages: List[Dict[str, Any]],
        system: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> Iterator[str]:
        try:
            from google.genai import types as gtypes

            client = self._get_client()
            contents = []
            for m in messages:
                role = m.get("role", "user")
                content = m.get("content", "")
                if role == "system":
                    system = content
                    continue
                contents.append(gtypes.Content(
                    role="user" if role == "user" else "model",
                    parts=[gtypes.Part(text=content)],
                ))

            config = gtypes.GenerateContentConfig(
                temperature=temperature,
                max_output_tokens=max_tokens,
            )
            if system:
                config.system_instruction = system

            for chunk in client.models.generate_content_stream(
                model=self._model,
                contents=contents,
                config=config,
            ):
                if chunk.text:
                    yield chunk.text
            self._health.record_success()
        except Exception as e:
            self._health.record_failure(str(e))
            log.warning("[GEMINI] stream error: %s", e)
            yield f"[Error: {e}]"

    def get_model_info(self) -> dict:
        return {
            "provider": "gemini",
            "model": self._model,
            "api_key_present": bool(self._api_key),
            "available": self.is_available(),
            "health": self.health.snapshot(),
        }
