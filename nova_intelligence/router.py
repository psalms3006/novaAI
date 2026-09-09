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
    provider_is_local,
)

log = logging.getLogger(__name__)


def _telemetry_call(provider, started: float, local: bool, *, status: str,
                    error_code: str | None = None) -> None:
    """Record one provider request. Never raises, never blocks."""
    try:
        import nova_account
        nova_account.emit_model_call(
            provider.name,
            getattr(provider, "model", "") or getattr(provider, "model_name", "")
            or provider.name,
            latency_ms=int((time.time() - started) * 1000),
            status=status, error_code=error_code, offline=local)
    except Exception:
        pass


def _telemetry_event(event: str, **attrs) -> None:
    try:
        import nova_account
        nova_account.emit(event, **attrs)
    except Exception:
        pass


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

    def rank_providers(
        self,
        require_tools: bool = False,
        require_vision: bool = False,
        tool_names: Optional[List[str]] = None,
    ) -> List[IntelligenceProvider]:
        """Return every usable provider, best first.

        Returning a ranked *list* rather than a single winner is what makes
        fallback real: :meth:`complete` and :meth:`stream` walk this list and
        try the next provider when one actually fails, instead of surfacing the
        first provider's error to the user as if nothing else existed.

        Ranking inputs: connectivity state, provider health, capability
        requirements, and the network requirements of the tools in play.
        """
        state = self._connectivity.state
        task_class = self._classify_task(tool_names)

        # ── Capability filter ─────────────────────────────────────────────────
        candidates = []
        for name, prov in self._providers.items():
            if require_tools and not (prov.capabilities & ProviderCapability.TOOL_CALLING):
                continue
            if require_vision and not (prov.capabilities & ProviderCapability.VISION):
                continue
            candidates.append((name, prov))

        if not candidates:
            log.warning(
                "[ROUTER] no provider matches requirements (tools=%s, vision=%s)",
                require_tools, require_vision,
            )
            return []

        # ── Availability filter ───────────────────────────────────────────────
        available = [(n, p) for n, p in candidates if p.is_available()]
        if not available:
            log.warning("[ROUTER] no provider available")
            return []

        # Healthy providers rank above unhealthy ones, but unhealthy ones stay
        # in the list as last resorts — a provider with 3 consecutive failures
        # is still better than answering "no provider available".
        healthy = [(n, p) for n, p in available if p.health.is_healthy]
        degraded = [(n, p) for n, p in available if not p.health.is_healthy]

        def _prefers_online() -> bool:
            """Should the cloud provider outrank the local one for this task?"""
            if task_class == TaskClassification.ONLINE:
                return state != ConnectivityState.OFFLINE
            if task_class == TaskClassification.LOCAL:
                return False
            if state == ConnectivityState.OFFLINE:
                return False
            return self._online_preferred

        online_first = _prefers_online()

        def _sort_key(item):
            name, prov = item
            is_remote = not provider_is_local(prov)
            # Providers of the preferred kind sort first; name breaks ties so
            # ordering stays deterministic across runs.
            return (0 if is_remote == online_first else 1, name.lower())

        ordered = sorted(healthy, key=_sort_key) + sorted(degraded, key=_sort_key)

        log.info(
            "[ROUTER] rank: state=%s online_preferred=%s task=%s online_first=%s order=%s",
            state.value, self._online_preferred, task_class.value, online_first,
            [n for n, _ in ordered],
        )
        return [p for _, p in ordered]

    def select_provider(
        self,
        require_tools: bool = False,
        require_vision: bool = False,
        tool_names: Optional[List[str]] = None,
    ) -> Optional[IntelligenceProvider]:
        """The single best provider for the current conditions, or None."""
        ranked = self.rank_providers(
            require_tools=require_tools,
            require_vision=require_vision,
            tool_names=tool_names,
        )
        return ranked[0] if ranked else None

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

    def _note_provider_used(self, name: str) -> None:
        old_provider = self._last_provider_used
        self._last_provider_used = name
        if old_provider and old_provider != name:
            log.info("[ROUTER] switched: %s -> %s", old_provider, name)
            for cb in self._switch_callbacks:
                try:
                    cb(old_provider, name)
                except Exception:
                    pass

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
        """Route a completion request, falling over to the next provider on failure."""
        ranked = self.rank_providers(
            require_tools=require_tools,
            require_vision=require_vision,
            tool_names=[t.get("name") for t in (tools or [])],
        )
        if not ranked:
            return GenerateResult(
                error="No intelligence provider available. Check Ollama or network connection.",
                provider="none",
            )

        errors: List[str] = []
        first_choice = ranked[0].name
        for provider in ranked:
            self._note_provider_used(provider.name)
            started = time.time()
            local = provider_is_local(provider)
            try:
                result = provider.complete(
                    messages=messages,
                    system=system,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except Exception as e:
                # A provider that raises is a provider that failed. Record it
                # against its health so ranking learns, then try the next one.
                try:
                    provider.health.record_failure(str(e))
                except Exception:
                    pass
                _telemetry_call(provider, started, local, status="error",
                                error_code=type(e).__name__)
                errors.append(f"{provider.name}: {e}")
                log.warning("[ROUTER] %s raised, trying next provider: %s", provider.name, e)
                continue

            if result.error:
                _telemetry_call(provider, started, local, status="error",
                                error_code="provider_error")
                errors.append(f"{provider.name}: {result.error}")
                log.warning(
                    "[ROUTER] %s returned error, trying next provider: %s",
                    provider.name, str(result.error)[:200],
                )
                continue

            _telemetry_call(provider, started, local, status="success")
            if provider.name != first_choice and local:
                # The preferred provider did not answer and a local model did.
                # That is the offline path, and it is worth being able to see
                # how often it happens.
                _telemetry_event("OFFLINE_FALLBACK", provider=provider.name)
            return result

        log.error("[ROUTER] all providers failed: %s", errors)
        return GenerateResult(
            error="; ".join(errors) or "All intelligence providers failed.",
            provider="none",
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
        """Route a streaming request, falling over to the next provider on failure.

        Failover only happens *before the first chunk reaches the caller*. Once
        text has been yielded, switching providers mid-answer would splice two
        different responses together, so a late failure is surfaced instead.
        """
        ranked = self.rank_providers(
            require_tools=require_tools,
            tool_names=[t.get("name") for t in (tools or [])],
        )
        if not ranked:
            raise RuntimeError(
                "No intelligence provider available. Check Ollama or network connection."
            )

        errors: List[str] = []
        for provider in ranked:
            self._note_provider_used(provider.name)
            emitted = False
            try:
                for chunk in provider.stream(
                    messages=messages,
                    system=system,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                ):
                    emitted = True
                    yield chunk
                if emitted:
                    return
            except Exception as e:
                try:
                    provider.health.record_failure(str(e))
                except Exception:
                    pass
                errors.append(f"{provider.name}: {e}")
                if emitted:
                    # Partial answer already delivered — do not restart with a
                    # different provider; report the truncation honestly.
                    log.error("[ROUTER] %s failed mid-stream: %s", provider.name, e)
                    yield f"\n\n[Response interrupted: {e}]"
                    return
                log.warning("[ROUTER] %s stream failed, trying next provider: %s", provider.name, e)
                continue

            # Stream completed without error but produced nothing. That is not
            # a provider outage — it usually means the model replied with a
            # non-text part (e.g. a function call), which the streaming API
            # drops. Retry the SAME provider non-streamed before demoting it,
            # so a healthy primary is never abandoned for a slow local model.
            log.info("[ROUTER] %s streamed no text; retrying non-streamed", provider.name)
            try:
                result = provider.complete(
                    messages=messages,
                    system=system,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except Exception as e:
                errors.append(f"{provider.name}: {e}")
                continue
            if result.error:
                errors.append(f"{provider.name}: {result.error}")
                continue
            if result.text:
                yield result.text
                return
            errors.append(f"{provider.name}: empty response")

        log.error("[ROUTER] all providers failed to stream: %s", errors)
        # Raise rather than yielding a diagnostic string: callers must be able
        # to tell a failure from an answer, and the user must never see a raw
        # bracketed error where NOVA's reply belongs.
        raise RuntimeError("; ".join(errors) or "All intelligence providers failed.")

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
