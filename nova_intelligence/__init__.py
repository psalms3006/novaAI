"""nova_intelligence — Unified intelligence routing for NOVA.

Provides a single entry point for all LLM interactions regardless of whether
NOVA is online (Gemini) or offline (Ollama). The rest of NOVA should call
nova_intelligence.complete() or nova_intelligence.stream() instead of directly
calling Gemini or Ollama.
"""
from __future__ import annotations

from .connectivity import ConnectivityManager, ConnectivityState
from .provider import (
    IntelligenceProvider,
    GenerateResult,
    ProviderCapability,
    ProviderHealth,
    NetworkRequirement,
    TaskClassification,
    TOOL_NETWORK_REQUIREMENTS,
)
from .ollama_provider import OllamaProvider
from .router import IntelligenceRouter, get_router
from .local_model_manager import LocalModelManager, get_model_manager
from .local_runtime import LocalRuntimeManager, RuntimeState
from .offline_knowledge import OfflineKnowledgeManager

__all__ = [
    "ConnectivityManager",
    "ConnectivityState",
    "IntelligenceProvider",
    "GenerateResult",
    "ProviderCapability",
    "ProviderHealth",
    "NetworkRequirement",
    "TaskClassification",
    "TOOL_NETWORK_REQUIREMENTS",
    "OllamaProvider",
    "IntelligenceRouter",
    "get_router",
    "LocalModelManager",
    "get_model_manager",
    "LocalRuntimeManager",
    "RuntimeState",
    "OfflineKnowledgeManager",
]
