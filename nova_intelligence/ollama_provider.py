"""nova_intelligence.ollama_provider — Ollama local inference provider.

Wraps Ollama's HTTP API behind the IntelligenceProvider protocol. Handles
model detection, health checking, tool-call format conversion, and graceful
degradation when Ollama is unavailable.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Iterator, List, Optional

import requests

from .provider import GenerateResult, ProviderCapability, ProviderHealth

log = logging.getLogger(__name__)

OLLAMA_DEFAULT_URL = "http://localhost:11434"


class OllamaProvider:
    """Ollama local inference provider."""

    def __init__(
        self,
        base_url: str = OLLAMA_DEFAULT_URL,
        model: str = "llama3.2",
        timeout: int = 0,
    ):
        # 8s was far too short to be a real fallback: a cold local model spends
        # most of that just loading weights into memory, so every offline
        # request failed with a read timeout right after the router had
        # correctly chosen Ollama. Availability probes stay short (3s, below);
        # this timeout covers actual generation.
        timeout = timeout or int(os.getenv("NOVA_OLLAMA_TIMEOUT", "") or 120)
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._health = ProviderHealth()
        self._capabilities = (
            ProviderCapability.CHAT
            | ProviderCapability.TOOL_CALLING
            | ProviderCapability.STREAMING
        )
        self._last_check_time = 0.0
        self._last_check_result = False

    @property
    def name(self) -> str:
        return f"ollama:{self._model}"

    @property
    def capabilities(self) -> ProviderCapability:
        return self._capabilities

    @property
    def health(self) -> ProviderHealth:
        return self._health

    @property
    def model(self) -> str:
        return self._model

    @model.setter
    def model(self, value: str) -> None:
        self._model = value

    def is_running(self) -> bool:
        """Is the Ollama process reachable? Uses 5s cache to avoid repeated timeouts."""
        now = time.time()
        if (now - self._last_check_time) < 5.0:
            return self._last_check_result
        try:
            r = requests.get(f"{self._base_url}/api/tags", timeout=3)
            self._last_check_result = r.status_code == 200
        except Exception:
            self._last_check_result = False
        self._last_check_time = now
        return self._last_check_result

    def is_available(self) -> bool:
        """Quick check: is Ollama running and does it have the model?"""
        if not self.is_running():
            return False
        try:
            r = requests.get(f"{self._base_url}/api/tags", timeout=3)
            if r.status_code != 200:
                return False
            models = [m.get("name", "") for m in r.json().get("models", [])]
            return any(self._model in m for m in models)
        except Exception:
            return False

    def list_models(self) -> List[Dict[str, Any]]:
        """List all locally installed Ollama models."""
        if not self.is_running():
            return []
        try:
            r = requests.get(f"{self._base_url}/api/tags", timeout=5)
            if r.status_code != 200:
                return []
            models = []
            for m in r.json().get("models", []):
                name = m.get("name", "")
                size = m.get("size", 0)
                models.append({
                    "name": name,
                    "size_bytes": size,
                    "size_gb": round(size / (1024**3), 2),
                    "modified": m.get("modified_at", ""),
                    "family": m.get("details", {}).get("family", ""),
                    "parameter_size": m.get("details", {}).get("parameter_size", ""),
                })
            return models
        except Exception:
            return []

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
            payload = self._build_payload(messages, system, tools, temperature, max_tokens)
            r = requests.post(
                f"{self._base_url}/api/chat",
                json=payload,
                timeout=self._timeout,
            )
            r.raise_for_status()
            data = r.json()
            latency = (time.time() - t0) * 1000

            text = ""
            tool_calls = []
            msg = data.get("message", {})
            text = msg.get("content", "")

            # Parse tool calls
            raw_tools = msg.get("tool_calls", [])
            for tc in raw_tools:
                func = tc.get("function", {})
                name = func.get("name", "")
                args = func.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                if name:
                    tool_calls.append({"name": name, "args": args})

            self._health.record_success(latency)
            return GenerateResult(
                text=text,
                tool_calls=tool_calls,
                provider="ollama",
                model=self._model,
                latency_ms=latency,
                raw=data,
            )
        except Exception as e:
            latency = (time.time() - t0) * 1000
            self._health.record_failure(str(e))
            log.warning("[OLLAMA] complete error: %s", e)
            return GenerateResult(
                error=str(e),
                provider="ollama",
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
            payload = self._build_payload(messages, system, tools, temperature, max_tokens)
            payload["stream"] = True
            with requests.post(
                f"{self._base_url}/api/chat",
                json=payload,
                timeout=self._timeout,
                stream=True,
            ) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                        content = chunk.get("message", {}).get("content", "")
                        if content:
                            yield content
                        if chunk.get("done"):
                            break
                    except json.JSONDecodeError:
                        continue
            self._health.record_success()
        except Exception as e:
            # Raise rather than yielding an error string: the router can only
            # fail over to another provider if the failure is visible as an
            # exception. A yielded "[Error: ...]" looks like a valid answer.
            self._health.record_failure(str(e))
            log.warning("[OLLAMA] stream error: %s", e)
            raise

    def get_model_info(self) -> dict:
        return {
            "provider": "ollama",
            "model": self._model,
            "base_url": self._base_url,
            "available": self.is_available(),
            "running": self.is_running(),
            "health": self.health.snapshot(),
        }

    def download_model(self, model_name: str) -> bool:
        """Pull a model from Ollama registry."""
        try:
            log.info("[OLLAMA] downloading model %s ...", model_name)
            r = requests.post(
                f"{self._base_url}/api/pull",
                json={"name": model_name},
                timeout=600,
                stream=True,
            )
            r.raise_for_status()
            for line in r.iter_lines():
                if line:
                    try:
                        status = json.loads(line)
                        if "error" in status:
                            log.error("[OLLAMA] pull error: %s", status["error"])
                            return False
                    except json.JSONDecodeError:
                        pass
            log.info("[OLLAMA] model %s downloaded", model_name)
            return True
        except Exception as e:
            log.error("[OLLAMA] download failed: %s", e)
            return False

    def remove_model(self, model_name: str) -> bool:
        try:
            r = requests.delete(
                f"{self._base_url}/api/delete",
                json={"name": model_name},
                timeout=10,
            )
            return r.status_code == 200
        except Exception:
            return False

    def test_model(self) -> dict:
        """Quick test: send a simple prompt and measure response."""
        result = self.complete(
            messages=[{"role": "user", "content": "Say hello in one word."}],
            system="You are a helpful assistant. Be brief.",
            max_tokens=20,
        )
        return {
            "ok": result.ok,
            "text": result.text[:100],
            "latency_ms": result.latency_ms,
            "error": result.error,
        }

    def _build_payload(
        self,
        messages: List[Dict[str, Any]],
        system: str,
        tools: Optional[List[Dict[str, Any]]],
        temperature: float,
        max_tokens: int,
    ) -> dict:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        for m in messages:
            msgs.append({"role": m.get("role", "user"), "content": m.get("content", "")})

        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": msgs,
            "stream": False,
            # Keep the model resident between turns. Ollama unloads after 5
            # minutes by default, and reloading a multi-GB model costs more
            # than the whole request budget — a cold mistral:latest exceeded a
            # 120s timeout on this machine, so the local fallback appeared
            # broken when it was only cold.
            "keep_alive": os.getenv("NOVA_OLLAMA_KEEP_ALIVE", "") or "30m",
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }

        if tools and not self._model.startswith("tinyllama"):
            payload["tools"] = self._convert_tools(tools)

        return payload

    def _convert_tools(self, gemini_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert Gemini-format tool declarations to Ollama format."""
        ollama_tools = []
        for tool in gemini_tools:
            if "function_declarations" in tool:
                for fd in tool["function_declarations"]:
                    ollama_tools.append({
                        "type": "function",
                        "function": {
                            "name": fd.get("name", ""),
                            "description": fd.get("description", ""),
                            "parameters": fd.get("parameters", {}),
                        },
                    })
            elif "name" in tool:
                ollama_tools.append({
                    "type": "function",
                    "function": {
                        "name": tool.get("name", ""),
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters", {}),
                    },
                })
        return ollama_tools
