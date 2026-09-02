"""test_intelligence_full.py — Comprehensive tests for nova_intelligence package.

Run: python test_intelligence_full.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

from nova_intelligence.connectivity import ConnectivityManager, ConnectivityState
from nova_intelligence.provider import (
    GenerateResult, ProviderCapability, ProviderHealth,
    NetworkRequirement, TaskClassification, TOOL_NETWORK_REQUIREMENTS,
)
from nova_intelligence.ollama_provider import OllamaProvider
from nova_intelligence.local_model_manager import LocalModelManager, KNOWN_MODELS, DEFAULT_MODEL_ID
from nova_intelligence.local_runtime import LocalRuntimeManager, RuntimeState
from nova_intelligence.router import IntelligenceRouter, init_router, get_router
from nova_intelligence.gemini_provider import GeminiProvider


def test_connectivity_offline():
    """ConnectivityManager with unreachable hosts should report OFFLINE."""
    mgr = ConnectivityManager(
        dns_hosts=("192.0.2.1",),
        dns_timeout=1.0,
        check_interval=999,
    )
    state = mgr.check_now()
    assert state == ConnectivityState.OFFLINE, f"expected OFFLINE, got {state}"
    assert mgr.is_offline
    snap = mgr.snapshot()
    assert snap["state"] == "offline"
    print("PASS: connectivity offline")


def test_provider_health():
    h = ProviderHealth()
    assert h.is_healthy
    assert h.failure_rate == 0.0
    h.record_success()
    h.record_success()
    assert h.success_count == 2
    assert h.consecutive_fails == 0
    for _ in range(3):
        h.record_failure("test error")
    assert not h.is_healthy
    assert h.consecutive_fails == 3
    snap = h.snapshot()
    assert snap["consecutive_fails"] == 3
    print("PASS: provider health")


def test_provider_health_recovery():
    h = ProviderHealth()
    for _ in range(3):
        h.record_failure("test")
    assert not h.is_healthy
    h.record_success()
    assert h.consecutive_fails == 0
    assert h.is_healthy
    print("PASS: provider health recovery")


def test_generate_result():
    r = GenerateResult(text="hello", provider="test")
    assert r.ok
    assert r.text == "hello"
    r2 = GenerateResult(error="fail")
    assert not r2.ok
    print("PASS: generate result")


def test_network_requirements():
    assert TOOL_NETWORK_REQUIREMENTS["open_app"] == NetworkRequirement.NONE
    assert TOOL_NETWORK_REQUIREMENTS["web_search"] == NetworkRequirement.OPTIONAL
    assert TOOL_NETWORK_REQUIREMENTS.get("nonexistent", NetworkRequirement.OPTIONAL) == NetworkRequirement.OPTIONAL
    print("PASS: network requirements")


def test_task_classification():
    router = IntelligenceRouter()
    assert router._classify_task(["open_app", "file_read"]) == TaskClassification.LOCAL
    assert router._classify_task(["web_search"]) == TaskClassification.HYBRID
    assert router._classify_task(None) == TaskClassification.HYBRID
    assert router._classify_task([]) == TaskClassification.HYBRID
    print("PASS: task classification")


def test_router_selection():
    conn = ConnectivityManager(dns_hosts=("192.0.2.1",), dns_timeout=1.0, check_interval=999)
    conn.check_now()
    router = IntelligenceRouter(connectivity=conn)
    
    ollama = OllamaProvider(model="llama3.2")
    gemini = GeminiProvider(api_key="test-key")
    router.register_provider("ollama", ollama)
    router.register_provider("gemini", gemini)
    router.set_online_preferred(False)
    
    # Offline: should select ollama
    prov = router.select_provider()
    assert prov is not None
    assert "ollama" in prov.name
    print("PASS: router selection (offline -> ollama)")


def test_router_capability_aware():
    conn = ConnectivityManager(dns_hosts=("192.0.2.1",), dns_timeout=1.0, check_interval=999)
    conn.check_now()
    router = IntelligenceRouter(connectivity=conn)
    
    ollama = OllamaProvider(model="llama3.2")
    router.register_provider("ollama", ollama)
    router.set_online_preferred(False)
    
    # LOCAL task should select ollama
    prov = router.select_provider(tool_names=["open_app", "file_read"])
    assert prov is not None
    assert "ollama" in prov.name
    print("PASS: router capability-aware routing")


def test_local_model_manager():
    mgr = LocalModelManager()
    status = mgr.get_status()
    assert "current_model" in status
    assert "known_models" in status
    assert len(status["known_models"]) > 0
    # Check that KNOWN_MODELS is not empty
    assert len(KNOWN_MODELS) > 0
    print("PASS: local model manager")


def test_local_runtime_detect():
    runtime = LocalRuntimeManager()
    state = runtime.detect()
    assert state in (RuntimeState.NOT_INSTALLED, RuntimeState.INSTALLED_NOT_RUNNING, RuntimeState.RUNNING)
    health = runtime.health_check()
    assert "state" in health
    assert "http_alive" in health
    print(f"PASS: local runtime detect ({state.name})")


def test_offline_knowledge():
    from nova_intelligence.offline_knowledge import OfflineKnowledgeManager, KNOWN_ZIM_PACKAGES
    mgr = OfflineKnowledgeManager()
    status = mgr.status()
    assert "installed" in status
    assert "available" in status
    assert len(KNOWN_ZIM_PACKAGES) > 0
    packages = mgr.available_packages()
    assert isinstance(packages, list)
    print("PASS: offline knowledge")


def test_ollama_provider_interface():
    p = OllamaProvider(model="llama3.2")
    assert p.name == "ollama:llama3.2"
    assert p.capabilities & ProviderCapability.TOOL_CALLING
    assert p.capabilities & ProviderCapability.STREAMING
    info = p.get_model_info()
    assert "provider" in info
    print("PASS: ollama provider interface")


def test_gemini_provider_interface():
    p = GeminiProvider(api_key="test")
    assert p.name == "gemini"
    assert p.capabilities & ProviderCapability.TOOL_CALLING
    assert p.capabilities & ProviderCapability.VISION
    info = p.get_model_info()
    assert info["provider"] == "gemini"
    print("PASS: gemini provider interface")


if __name__ == "__main__":
    tests = [
        test_connectivity_offline,
        test_provider_health,
        test_provider_health_recovery,
        test_generate_result,
        test_network_requirements,
        test_task_classification,
        test_router_selection,
        test_router_capability_aware,
        test_local_model_manager,
        test_local_runtime_detect,
        test_offline_knowledge,
        test_ollama_provider_interface,
        test_gemini_provider_interface,
    ]
    
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"FAIL: {t.__name__}: {e}")
            failed += 1
    
    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed, {len(tests)} total")
    if failed:
        sys.exit(1)
