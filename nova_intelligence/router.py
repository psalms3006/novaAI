"""nova_intelligence.router — Intelligence routing for NOVA.

Selects the best available provider based on connectivity, provider health,
model capabilities, and user preferences. Provides the main entry point for
all LLM interactions.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Iterator, List, Optional

from .connectivity import ConnectivityManager, ConnectivityState
from .ollama_provider import OllamaProvider
from .provider import (
    GenerateResult, IntelligenceProvider, ProviderCapability, ProviderHealth,
    TaskClassification, NetworkRequirement, TOOL_NETWORK_REQUIREMENTS,
)

log = logging.getLogger(__name__)


class IntelligenceRouter:
    """Selects and delegates to the best available intelligence provider.

    The router holds references to all providers and the connectivity manager.
    On each request it picks the best provider based on:

    1. Connectivity state (online/offline/degraded)
    2. Provider availability (is_available())
    3. Provider health (consecutive failures)
    4. User preference (online_preferred setting)
    5. Task requirements (tool calling, vision, etc.)
    """

    def __init__(self, connectivity: Optional[ConnectivityManager] = None):
        self._connectivity = connectivity or ConnectivityManager()
        self._providers: Dict[str, IntelligenceProvider] = {}
        self._online_preferred = True
        self._last_provider_used = ""
        self._switch_callbacks: List[Callable] = []

    @property
    def connectivity(self) -> ConnectivityManager:
        return self._connectivity

    def register_provider(self, name: str, provider: IntelligenceProvider) -> None:
        self._providers[name] = provider
        log.info("[ROUTER] registered provider: %s", name)

    def set_online_preferred(self, preferred: bool) -> None:
        self._online_preferred = preferred

    def on_provider_switch(self, callback: Callable) -> None:
        self._switch_callbacks.append(callback)

    def get_provider(self, name: str) -> Optional[IntelligenceProvider]:
        return self._providers.get(name)

    def select_provider(
        self,
        require_tools: bool = False,
        require_vision: bool = False,
        tool_names: Optional[List[str]] = None,
    ) -> Optional[IntelligenceProvider]:
        """Select the best provider for the current conditions.
        
        Uses connectivity state, provider health, task requirements, and
        tool network requirements to make intelligent routing decisions.
        """
        state = self._connectivity.state
+        # ---- DEBUG ----
+        # Show whether we have a Gemini provider registered
+        gemini_present = "set" if self._providers.get("gemini") is not None else "None"
+        print(f"[DEBUG] gemini_provider: {gemini_present}")
+        # Show current connectivity state
+        print(f"[DEBUG] connectivity_state: {state.name}")
+        # Show online preference flag
+        print(f"[DEBUG] online_preferred: {self._online_preferred}")
+        # ---- END DEBUG ----

        # Classify the task based on tool requirements
        task_class = self._classify_task(tool_names)

        # Filter by capability requirements
        candidates = []
        for name, prov in self._providers.items():
            if require_tools and not (prov.capabilities & ProviderCapability.TOOL_CALLING):
                continue
            if require_vision and not (prov.capabilities & ProviderCapability.VISION):
                continue
            candidates.append((name, prov))

        if not candidates:
            log.warning("[ROUTER] no provider matches requirements (tools=%s, vision=%s)", require_tools, require_vision)
            return None

        # ── Health-aware routing ──────────────────────────────────────────────
        # Filter to only healthy providers
        healthy = [(n, p) for n, p in candidates if p.health.is_healthy and p.is_available()]
        if not healthy:
            # Fall back to any available provider (even unhealthy ones)
            log.warning("[ROUTER] no healthy providers; trying unhealthy ones")
            healthy = [(n, p) for n, p in candidates if p.is_available()]
        
        if not healthy:
            log.warning("[ROUTER] no provider available")
            return None

        # ── Task-based selection ──────────────────────────────────────────────

        # LOCAL task: prefer Ollama (it's optimized for tool-heavy local work)
        if task_class == TaskClassification.LOCAL:
            for name, prov in healthy:
                if "ollama" in name.lower():
                    debug_branch = "LOCAL -> Ollama"
                    print(f"[DEBUG] branch: {debug_branch}")
                    return prov
            # If no Ollama, any healthy provider will do
            debug_branch = "LOCAL -> any healthy"
            print(f"[DEBUG] branch: {debug_branch}")
            # will fall through to later fallback
 
        # ONLINE task: prefer Gemini (cloud capabilities needed)
        if task_class == TaskClassification.ONLINE:
            if state != ConnectivityState.OFFLINE:
                for name, prov in healthy:
                    if "gemini" in name.lower():
                        debug_branch = "ONLINE -> Gemini"
                        print(f"[DEBUG] branch: {debug_branch}")
                        return prov
            # No Gemini found
            debug_branch = "ONLINE -> any healthy"
            print(f"[DEBUG] branch: {debug_branch}")
 
        # HYBRID or fallback: use connectivity + preference
        if self._online_preferred and state == ConnectivityState.ONLINE:
            for name, prov in healthy:
                if "gemini" in name.lower():
                    debug_branch = "HYBRID (online preferred) -> Gemini"
                    print(f"[DEBUG] branch: {debug_branch}")
                    return prov
            # No Gemini found in HYBRID preference
            debug_branch = "HYBRID (online preferred) -> any healthy"
            print(f"[DEBUG] branch: {debug_branch}")
 
        if state == ConnectivityState.DEGRADED:
            # Degraded: check if the previously-used online provider is still healthy
            if self._last_provider_used:
                prev = self._providers.get(self._last_provider_used)
                if prev and prev.health.is_healthy and prev.is_available():
                    debug_branch = "DEGRADED -> previous provider"
                    print(f"[DEBUG] branch: {debug_branch}")
                    return prev
            # Fall through to local preference
 
        if state in (ConnectivityState.OFFLINE, ConnectivityState.DEGRADED):
            for name, prov in healthy:
                if "ollama" in name.lower():
                    debug_branch = "OFFLINE/DEGRADED -> Ollama"
                    print(f"[DEBUG] branch: {debug_branch}")
                    return prov
            debug_branch = "OFFLINE/DEGRADED -> any healthy"
            print(f"[DEBUG] branch: {debug_branch}")
 
        # Final fallback: first healthy provider
        debug_branch = "FINAL fallback"
        print(f"[DEBUG] branch: {debug_branch}")
        print(f"[DEBUG] final provider: {healthy[0][0] if healthy else 'None'}")
        return healthy[0][1] if healthy else None

    def _classify_task(self, tool_names: Optional[List[str]] = None) -> TaskClassification:
        """Classify a task based on which tools it uses.
        
        - LOCAL: All tools are purely local (file ops, memory, etc.)
        - ONLINE: At least one tool requires network
        - HYBRID: Mix of local and optional-network tools
        """
        if not tool_names:
            return TaskClassification.HYBRID

        has_network_required = False
        has_network_optional = False
        has_local_only = False

        for name in tool_names:
            req = TOOL_NETWORK_REQUIREMENTS.get(name, NetworkRequirement.OPTIONAL)
            if req == NetworkRequirement.REQUIRED:
                has_network_required = True
            elif req == NetworkRequirement.OPTIONAL:
                has_network_optional = True
            else:
                has_local_only = True

        if has_network_required:
            return TaskClassification.ONLINE
        if has_local_only and not has_network_optional:
            return TaskClassification.LOCAL
        return TaskClassification.HYBRID

    def complete(
        self,
        messages: List[Dict[str, Any]],
        system: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        require_tools: bool = False,
        require_vision: bool = False,
    ) -> GenerateResult:
        """Route a completion request to the best provider."""
        provider = self.select_provider(
            require_tools=require_tools,
            require_vision=require_vision,
        )
        if provider is None:
            return GenerateResult(
                error="No intelligence provider available. Check Ollama or network connection.",
                provider="none",
            )

        old_provider = self._last_provider_used
        self._last_provider_used = provider.name

        # Notify on provider switch
        if old_provider and old_provider != provider.name:
            log.info("[ROUTER] switched: %s -> %s", old_provider, provider.name)
            for cb in self._switch_callbacks:
                try:
                    cb(old_provider, provider.name)
                except Exception:
                    pass

        return provider.complete(
            messages=messages,
            system=system,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def stream(
        self,
        messages: List[Dict[str, Any]],
        system: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        require_tools: bool = False,
    ) -> Iterator[str]:
        """Route a streaming request to the best provider."""
        provider = self.select_provider(require_tools=require_tools)
        if provider is None:
            yield "[No intelligence provider available]"
            return

        old_provider = self._last_provider_used
        self._last_provider_used = provider.name
        if old_provider and old_provider != provider.name:
            for cb in self._switch_callbacks:
                try:
                    cb(old_provider, provider.name)
                except Exception:
                    pass

        yield from provider.stream(
            messages=messages,
            system=system,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def snapshot(self) -> dict:
        """Full diagnostics snapshot."""
        providers = {}
        for name, prov in self._providers.items():
            providers[name] = {
                "available": prov.is_available(),
                "healthy": prov.health.is_healthy,
                "capabilities": str(prov.capabilities),
                "health": prov.health.snapshot(),
            }
        return {
            "connectivity": self._connectivity.snapshot(),
            "providers": providers,
            "last_provider": self._last_provider_used,
            "online_preferred": self._online_preferred,
        }


# ── Module singleton ──────────────────────────────────────────────────────────
_router: Optional[IntelligenceRouter] = None


def get_router() -> IntelligenceRouter:
    global _router
    if _router is None:
        _router = IntelligenceRouter()
    return _router


def init_router(
    gemini_provider: Optional[IntelligenceProvider] = None,
    ollama_provider: Optional[OllamaProvider] = None,
    connectivity: Optional[ConnectivityManager] = None,
) -> IntelligenceRouter:
    """Initialize the global router with providers."""
    global _router
    _router = IntelligenceRouter(connectivity=connectivity or ConnectivityManager())
    if gemini_provider:
        _router.register_provider("gemini", gemini_provider)
    if ollama_provider:
        _router.register_provider("ollama", ollama_provider)
    return _router
