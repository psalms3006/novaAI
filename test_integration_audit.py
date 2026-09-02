"""test_integration_audit.py — Integration audit tests.
Run: python test_integration_audit.py
"""
import sys, os, time, threading
sys.path.insert(0, os.path.dirname(__file__))

results = []

def test(name, fn):
    try:
        fn()
        results.append((name, "PASS"))
        print(f"  PASS: {name}")
    except AssertionError as e:
        results.append((name, f"FAIL: {e}"))
        print(f"  FAIL: {name}: {e}")
    except Exception as e:
        results.append((name, f"ERROR: {e}"))
        print(f"  ERROR: {name}: {e}")


def t_local_runtime():
    from nova_intelligence.local_runtime import LocalRuntimeManager, RuntimeState
    rt = LocalRuntimeManager()
    state = rt.detect()
    assert state in (RuntimeState.NOT_INSTALLED, RuntimeState.INSTALLED_NOT_RUNNING, RuntimeState.RUNNING)
    health = rt.health_check()
    assert "state" in health
    assert "http_alive" in health

def t_ollama_running():
    from nova_intelligence.ollama_provider import OllamaProvider
    p = OllamaProvider(model="tinyllama:latest")
    assert p.is_running(), "Ollama not running"
    assert p.is_available(), "tinyllama not available"

def t_ollama_basic_prompt():
    from nova_intelligence.ollama_provider import OllamaProvider
    p = OllamaProvider(model="tinyllama:latest")
    r = p.complete(
        messages=[{"role": "user", "content": "What is 2+2?"}],
        max_tokens=20,
    )
    assert r.ok, f"Ollama prompt failed: {r.error}"
    assert len(r.text) > 0, "Empty response"

def t_ollama_streaming():
    from nova_intelligence.ollama_provider import OllamaProvider
    p = OllamaProvider(model="tinyllama:latest")
    chunks = list(p.stream(
        messages=[{"role": "user", "content": "Say hi"}],
        max_tokens=10,
    ))
    assert len(chunks) > 0, "No streaming chunks received"

def t_router_offline():
    from nova_intelligence.connectivity import ConnectivityManager, ConnectivityState
    from nova_intelligence.router import IntelligenceRouter
    from nova_intelligence.ollama_provider import OllamaProvider
    from nova_intelligence.gemini_provider import GeminiProvider
    conn = ConnectivityManager(check_interval=999)
    conn._state = ConnectivityState.OFFLINE
    ollama = OllamaProvider(model="tinyllama:latest")
    gemini = GeminiProvider(api_key="test")
    router = IntelligenceRouter(connectivity=conn)
    router.register_provider("ollama", ollama)
    router.register_provider("gemini", gemini)
    router.set_online_preferred(True)
    p = router.select_provider()
    assert "ollama" in p.name, f"Expected ollama, got {p.name}"

def t_router_online():
    from nova_intelligence.connectivity import ConnectivityManager, ConnectivityState
    from nova_intelligence.router import IntelligenceRouter
    from nova_intelligence.ollama_provider import OllamaProvider
    from nova_intelligence.gemini_provider import GeminiProvider
    conn = ConnectivityManager(check_interval=999)
    conn._state = ConnectivityState.ONLINE
    ollama = OllamaProvider(model="tinyllama:latest")
    gemini = GeminiProvider(api_key="test")
    gemini.health.record_success()
    router = IntelligenceRouter(connectivity=conn)
    router.register_provider("ollama", ollama)
    router.register_provider("gemini", gemini)
    router.set_online_preferred(True)
    p = router.select_provider()
    assert "gemini" in p.name, f"Expected gemini, got {p.name}"

def t_router_degraded_healthy():
    from nova_intelligence.connectivity import ConnectivityManager, ConnectivityState
    from nova_intelligence.router import IntelligenceRouter
    from nova_intelligence.ollama_provider import OllamaProvider
    from nova_intelligence.gemini_provider import GeminiProvider
    conn = ConnectivityManager(check_interval=999)
    conn._state = ConnectivityState.DEGRADED
    ollama = OllamaProvider(model="tinyllama:latest")
    gemini = GeminiProvider(api_key="test")
    gemini.health.record_success()
    router = IntelligenceRouter(connectivity=conn)
    router.register_provider("ollama", ollama)
    router.register_provider("gemini", gemini)
    router.set_online_preferred(True)
    router._last_provider_used = "gemini"
    p = router.select_provider()
    assert "gemini" in p.name, f"Expected gemini (healthy), got {p.name}"

def t_router_degraded_unhealthy():
    from nova_intelligence.connectivity import ConnectivityManager, ConnectivityState
    from nova_intelligence.router import IntelligenceRouter
    from nova_intelligence.ollama_provider import OllamaProvider
    from nova_intelligence.gemini_provider import GeminiProvider
    conn = ConnectivityManager(check_interval=999)
    conn._state = ConnectivityState.DEGRADED
    ollama = OllamaProvider(model="tinyllama:latest")
    gemini = GeminiProvider(api_key="test")
    for _ in range(3):
        gemini.health.record_failure("timeout")
    assert not gemini.health.is_healthy
    router = IntelligenceRouter(connectivity=conn)
    router.register_provider("ollama", ollama)
    router.register_provider("gemini", gemini)
    router.set_online_preferred(True)
    p = router.select_provider()
    assert "ollama" in p.name, f"Expected ollama (unhealthy gemini), got {p.name}"

def t_router_capability_local():
    from nova_intelligence.connectivity import ConnectivityManager, ConnectivityState
    from nova_intelligence.router import IntelligenceRouter
    from nova_intelligence.ollama_provider import OllamaProvider
    from nova_intelligence.gemini_provider import GeminiProvider
    conn = ConnectivityManager(check_interval=999)
    conn._state = ConnectivityState.ONLINE
    ollama = OllamaProvider(model="tinyllama:latest")
    gemini = GeminiProvider(api_key="test")
    gemini.health.record_success()
    router = IntelligenceRouter(connectivity=conn)
    router.register_provider("ollama", ollama)
    router.register_provider("gemini", gemini)
    router.set_online_preferred(True)
    p = router.select_provider(tool_names=["open_app", "file_read"])
    assert "ollama" in p.name, f"Expected ollama for LOCAL task, got {p.name}"

def t_task_classification():
    from nova_intelligence.router import IntelligenceRouter
    from nova_intelligence.provider import TaskClassification
    router = IntelligenceRouter()
    assert router._classify_task(["open_app", "file_read"]) == TaskClassification.LOCAL
    assert router._classify_task(["web_search"]) == TaskClassification.HYBRID
    assert router._classify_task(["web_api_call"]) == TaskClassification.ONLINE
    assert router._classify_task(None) == TaskClassification.HYBRID

def t_provider_health_deadlock():
    from nova_intelligence.provider import ProviderHealth
    h = ProviderHealth()
    s = h.snapshot()
    assert "healthy" in s
    for _ in range(5):
        h.record_failure("x")
    s = h.snapshot()
    assert not s["healthy"]
    h.record_success()
    s = h.snapshot()
    assert s["healthy"]

def t_offline_knowledge():
    from nova_intelligence.offline_knowledge import OfflineKnowledgeManager
    mgr = OfflineKnowledgeManager()
    status = mgr.status()
    assert "installed" in status
    assert "available" in status
    assert isinstance(mgr.available_packages(), list)

def t_model_manager():
    from nova_intelligence.local_model_manager import LocalModelManager, KNOWN_MODELS
    mgr = LocalModelManager()
    status = mgr.get_status()
    assert "current_model" in status
    assert len(KNOWN_MODELS) >= 3

def t_voice_provider():
    from nova_intelligence.voice_provider import VoiceProvider
    vp = VoiceProvider()
    mode = vp.detect()
    status = vp.status()
    assert "mode" in status

def t_router_streaming():
    from nova_intelligence.connectivity import ConnectivityManager, ConnectivityState
    from nova_intelligence.router import IntelligenceRouter
    from nova_intelligence.ollama_provider import OllamaProvider
    conn = ConnectivityManager(check_interval=999)
    conn._state = ConnectivityState.OFFLINE
    ollama = OllamaProvider(model="tinyllama:latest")
    router = IntelligenceRouter(connectivity=conn)
    router.register_provider("ollama", ollama)
    router.set_online_preferred(False)
    chunks = list(router.stream(
        messages=[{"role": "user", "content": "Say hi"}],
        max_tokens=10,
    ))
    assert len(chunks) > 0, "No streaming chunks from router"


if __name__ == "__main__":
    print("=" * 60)
    print("INTEGRATION AUDIT TESTS")
    print("=" * 60)
    
    test("LocalRuntimeManager detect", t_local_runtime)
    test("Ollama running", t_ollama_running)
    test("Ollama basic prompt", t_ollama_basic_prompt)
    test("Ollama streaming", t_ollama_streaming)
    test("Router OFFLINE -> ollama", t_router_offline)
    test("Router ONLINE -> gemini", t_router_online)
    test("Router DEGRADED (healthy) -> gemini", t_router_degraded_healthy)
    test("Router DEGRADED (unhealthy) -> ollama", t_router_degraded_unhealthy)
    test("Router LOCAL task -> ollama", t_router_capability_local)
    test("Task classification", t_task_classification)
    test("ProviderHealth no deadlock", t_provider_health_deadlock)
    test("Offline knowledge", t_offline_knowledge)
    test("Model manager", t_model_manager)
    test("Voice provider", t_voice_provider)
    test("Router streaming", t_router_streaming)
    
    print()
    print("=" * 60)
    passed = sum(1 for _, r in results if r == "PASS")
    failed = sum(1 for _, r in results if r != "PASS")
    print(f"Results: {passed} passed, {failed} failed, {len(results)} total")
    print("=" * 60)
    for name, r in results:
        status = "PASS" if r == "PASS" else "FAIL"
        print(f"  [{status}] {name}" if r == "PASS" else f"  [FAIL] {name}: {r}")
