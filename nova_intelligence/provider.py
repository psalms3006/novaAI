"""nova_intelligence.provider — Intelligence provider protocol and types.

Defines the common interface that all LLM providers (Gemini, Ollama, etc.)
must implement. The router and the rest of NOVA interact only through this
interface — never directly with provider-specific code.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, Flag, auto
from typing import Any, Callable, Dict, Iterator, List, Optional, Protocol, runtime_checkable


class ProviderCapability(Flag):
    """Capabilities a provider may support."""
    NONE = 0
    CHAT = auto()
    TOOL_CALLING = auto()
    VISION = auto()
    STREAMING = auto()
    LIVE_AUDIO = auto()
    SYSTEM_INSTRUCTION = auto()


class NetworkRequirement(Enum):
    """How much network a tool/task requires."""
    NONE = "none"           # Purely local (open_app, file ops, memory)
    OPTIONAL = "optional"   # Works offline, enhanced online (web search, vision)
    REQUIRED = "required"   # Must be online (Gemini Live, web API calls)


class TaskClassification(Enum):
    """Classifies what a task needs from the provider."""
    LOCAL = "local"         # Tool-heavy, context-dependent, can run locally
    ONLINE = "online"       # Needs cloud capabilities (vision, large context)
    HYBRID = "hybrid"       # Can run either way, online preferred


# Tool metadata: maps tool name -> network requirement
TOOL_NETWORK_REQUIREMENTS: Dict[str, NetworkRequirement] = {
    # Purely local tools
    "open_app": NetworkRequirement.NONE,
    "close_app": NetworkRequirement.NONE,
    "file_read": NetworkRequirement.NONE,
    "file_write": NetworkRequirement.NONE,
    "file_edit": NetworkRequirement.NONE,
    "file_delete": NetworkRequirement.NONE,
    "file_move": NetworkRequirement.NONE,
    "file_copy": NetworkRequirement.NONE,
    "file_list": NetworkRequirement.NONE,
    "file_search": NetworkRequirement.NONE,
    "computer_settings": NetworkRequirement.NONE,
    "remember_fact": NetworkRequirement.NONE,
    "nova_memory": NetworkRequirement.NONE,
    "planner": NetworkRequirement.NONE,
    "nova_task": NetworkRequirement.NONE,
    "self_editor": NetworkRequirement.NONE,
    "screen_capture": NetworkRequirement.NONE,
    # Enhanced by network
    "web_search": NetworkRequirement.OPTIONAL,
    "vision": NetworkRequirement.OPTIONAL,
    "screen_understand": NetworkRequirement.OPTIONAL,
    "browser_navigate": NetworkRequirement.OPTIONAL,
    # Require network
    "web_api_call": NetworkRequirement.REQUIRED,
}


class ProviderHealth:
    """Tracks provider health for routing decisions."""

    def __init__(self):
        self.success_count = 0
        self.fail_count = 0
        self.last_success = 0.0
        self.last_failure = 0.0
        self.last_error = ""
        self.consecutive_fails = 0
        self._lock = __import__("threading").Lock()

    def record_success(self, latency: float = 0.0) -> None:
        with self._lock:
            self.success_count += 1
            self.last_success = time.time()
            self.consecutive_fails = 0

    def record_failure(self, error: str = "") -> None:
        with self._lock:
            self.fail_count += 1
            self.last_failure = time.time()
            self.last_error = error
            self.consecutive_fails += 1

    @property
    def is_healthy(self) -> bool:
        with self._lock:
            if self.consecutive_fails >= 3:
                return False
            if self.fail_count == 0:
                return True
            return self.consecutive_fails < 3

    @property
    def failure_rate(self) -> float:
        with self._lock:
            total = self.success_count + self.fail_count
            if total == 0:
                return 0.0
            return self.fail_count / total

    def snapshot(self) -> dict:
        with self._lock:
            # Inline is_healthy/failure_rate to avoid re-acquiring the lock (deadlock)
            healthy = True
            if self.consecutive_fails >= 3:
                healthy = False
            elif self.fail_count == 0:
                healthy = True
            else:
                healthy = self.consecutive_fails < 3

            total = self.success_count + self.fail_count
            fail_rate = self.fail_count / total if total > 0 else 0.0

            return {
                "success": self.success_count,
                "fail": self.fail_count,
                "consecutive_fails": self.consecutive_fails,
                "healthy": healthy,
                "last_error": self.last_error,
                "failure_rate": round(fail_rate, 3),
            }


@dataclass
class GenerateResult:
    """Unified result from any provider."""
    text: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    finish_reason: str = ""
    provider: str = ""
    model: str = ""
    latency_ms: float = 0.0
    error: str = ""
    raw: Any = None

    @property
    def ok(self) -> bool:
        return not self.error and (self.text or self.tool_calls)


def provider_is_local(provider: Any) -> bool:
    """Whether *provider* runs on this machine.

    Routing needs to know "local vs remote", not "is it Gemini". The router
    used to test `"gemini" in name`, which silently made every other cloud
    provider rank as local and would have mis-routed the moment a second one
    was registered. Providers declare `is_local`; the name heuristic is only a
    fallback for third-party providers that predate the attribute.
    """
    declared = getattr(provider, "is_local", None)
    if isinstance(declared, bool):
        return declared
    name = str(getattr(provider, "name", "")).lower()
    return any(tok in name for tok in ("ollama", "local", "llama.cpp", "llamacpp"))


@runtime_checkable
class IntelligenceProvider(Protocol):
    """Protocol that all intelligence providers must implement."""

    @property
    def name(self) -> str: ...

    #: True when inference happens on this machine. Used for network-aware
    #: routing; see :func:`provider_is_local`.
    is_local: bool

    @property
    def capabilities(self) -> ProviderCapability: ...

    @property
    def health(self) -> ProviderHealth: ...

    def is_available(self) -> bool:
        """Quick check: can this provider handle requests right now?"""
        ...

    def complete(
        self,
        messages: List[Dict[str, Any]],
        system: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> GenerateResult:
        """Synchronous completion. Returns GenerateResult."""
        ...

    def stream(
        self,
        messages: List[Dict[str, Any]],
        system: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> Iterator[str]:
        """Streaming completion. Yields text chunks."""
        yield ""

    def get_model_info(self) -> dict:
        """Return model metadata for diagnostics."""
        ...
