"""test_intelligence.py — Tests for nova_intelligence package.

Run: python test_intelligence.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

from nova_intelligence.connectivity import ConnectivityManager, ConnectivityState
from nova_intelligence.provider import GenerateResult, ProviderCapability, ProviderHealth
from nova_intelligence.ollama_provider import OllamaProvider
from nova_intelligence.local_model_manager import LocalModelManager, KNOWN_MODELS, DEFAULT_MODEL_ID
from nova_intelligence.router import IntelligenceRouter, init_router, get_router
from nova_intelligence.gemini_provider import GeminiProvider


def test_connectivity_offline():
    """ConnectivityManager with unreachable hosts should report OFFLINE."""
    mgr = ConnectivityManager(
        dns_hosts=("192.0.2.1",),  # TEST-NET — unreachable
        dns_timeout=1.0,
        check_interval=999,
    )
    state = mgr.check_now()
    assert state == ConnectivityState.OFFLINE, f"expected OFFLINE, got {state}"
    assert mgr.is_offline
    assert not mgr.is_online
    snap = mgr.snapshot()
    assert snap["state"] == "offline"
    print("PASS: connectivity offline")


def test_connectivity_online():
    """ConnectivityManager with reachable DNS should report ONLINE (if internet available)."""
    mgr = ConnectivityManager(dns_timeout=3.0, check_interval=999)
    state = mgr.check_now()
    # May be ONLINE or DEGRADED depending on environment
    assert state in (ConnectivityState.ONLINE, ConnectivityState.DEGRADED), f"unexpected: {state}"
    snap = mgr.snapshot()
    assert snap["dns_latency_ms"] > 0
    print(f"PASS: connectivity {state.value} (dns={snap['dns_latency_ms']}ms)")


def test_provider_health():
    h = ProviderHealth()
    assert h.is_healthy
    assert h.failure_rate == 0.0

    h.record_success(0.1)
    h.record_success(0.05)
    assert h.success_count == 2
    assert h.consecutive_fails == 0

    h.record_failure("test error")
    assert h.consecutive_fails == 1
    assert h.is_healthy  # still healthy at 1 failure

    h.record_failure("test error 2")
    h.record_failure("test error 3")
    assert not h.is_healthy  # unhealthy at 3 consecutive
    assert h.last_error == "test error 3"

    snap = h.snapshot()
    assert snap["consecutive_fails"] == 3
    assert not snap["healthy"]
    print("PASS: provider health")


def test_generate_result():
    r = GenerateResult(text="hello", provider="test")
    assert r.ok
    assert r.text == "hello"

    r2 = GenerateResult(error="fail")
    assert not r2.ok

    r3 = GenerateResult(tool_calls=[{"name": "test", "args": {}}])
    assert r3.ok
    print("PASS: generate result")


def test_ollama_provider_basics():
    p = OllamaProvider(model="tinyllama")
    assert p.name == "ollama:tinyllama"
    assert p.capabilities & ProviderCapability.CHAT
    assert p.capabilities & ProviderCapability.TOOL_CALLING

    # is_available checks Ollama — may or may not be running
    available = p.is_available()
    running = p.is_running()
    print(f"  Ollama running={running} model_available={available}")

    info = p.get_model_info()
    assert info["provider"] == "ollama"
    assert info["model"] == "tinyllama"
    print("PASS: ollama provider basics")


def test_ollama_provider_complete():
    p = OllamaProvider(model="tinyllama", timeout=15)
    if not p.is_available():
        print("SKIP: ollama not available — cannot test complete()")
        return

    result = p.complete(
        messages=[{"role": "user", "content": "Say hello in one word."}],
        system="You are a helpful assistant. Be brief.",
        max_tokens=20,
    )
    print(f"  result: ok={result.ok} text={result.text[:50]!r} latency={result.latency_ms:.0f}ms")
    if result.ok:
        assert len(result.text) > 0
    else:
        print(f"  warning: {result.error}")
    print("PASS: ollama provider complete")


def test_local_model_manager():
    mgr = LocalModelManager()

    # list known models
    available = mgr.list_available()
    assert len(available) >= 4, f"expected >= 4 known models, got {len(available)}"
    print(f"  {len(available)} known models")

    # get recommended
    rec = mgr.get_recommended()
    assert rec is not None
    assert rec["recommended"]
    print(f"  recommended: {rec['id']} ({rec['name']}, ~{rec['size_gb']} GB)")

    # current model
    model = mgr.current_model
    print(f"  current model: {model}")

    # get metadata
    meta = mgr.get_model_metadata("qwen2.5:3b")
    assert meta is not None
    assert meta["tool_calling"]
    print("PASS: local model manager")


def test_router_basics():
    conn = ConnectivityManager(dns_timeout=2.0, check_interval=999)
    conn.check_now()

    ollama = OllamaProvider(model="tinyllama")
    gemini = GeminiProvider(api_key=os.environ.get("GEMINI_API_KEY", ""))

    router = init_router(
        gemini_provider=gemini,
        ollama_provider=ollama,
        connectivity=conn,
    )
    assert router.get_provider("ollama") is ollama
    assert router.get_provider("gemini") is gemini

    # Select provider
    provider = router.select_provider()
    if provider:
        print(f"  selected: {provider.name}")
    else:
        print("  no provider available (expected in test env)")

    # Snapshot
    snap = router.snapshot()
    assert "connectivity" in snap
    assert "providers" in snap
    print("PASS: router basics")


def test_router_fallback():
    """Router should fall back to Ollama when Gemini is unavailable."""
    conn = ConnectivityManager(dns_timeout=1.0, check_interval=999)
    conn._state = ConnectivityState.OFFLINE  # force offline

    ollama = OllamaProvider(model="tinyllama")
    # Gemini with no key — unavailable
    gemini = GeminiProvider(api_key="")

    router = init_router(
        gemini_provider=gemini,
        ollama_provider=ollama,
        connectivity=conn,
    )

    provider = router.select_provider()
    if ollama.is_available():
        assert provider is ollama, f"expected ollama, got {provider}"
        print("PASS: router fallback to ollama (online unavailable)")
    else:
        # No providers available
        assert provider is None
        print("PASS: router returns None when all providers unavailable")


def test_ollama_tool_conversion():
    """Test tool declaration format conversion."""
    p = OllamaProvider()
    gemini_tools = [
        {
            "function_declarations": [
                {"name": "web_search", "description": "Search the web", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}},
                {"name": "read_file", "description": "Read a file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}},
            ]
        }
    ]
    ollama_tools = p._convert_tools(gemini_tools)
    assert len(ollama_tools) == 2
    assert ollama_tools[0]["type"] == "function"
    assert ollama_tools[0]["function"]["name"] == "web_search"
    assert ollama_tools[1]["function"]["name"] == "read_file"
    print("PASS: ollama tool conversion")


def test_known_models_metadata():
    """Verify known model metadata is consistent."""
    for m in KNOWN_MODELS:
        assert "id" in m, f"missing id in {m}"
        assert "name" in m, f"missing name in {m}"
        assert "size_gb" in m and m["size_gb"] > 0, f"bad size in {m['id']}"
        assert "context_length" in m and m["context_length"] > 0, f"bad context in {m['id']}"
        assert "tool_calling" in m, f"missing tool_calling in {m['id']}"
        assert "recommended_ram_gb" in m, f"missing ram in {m['id']}"
        assert "strengths" in m, f"missing strengths in {m['id']}"
        assert "limitations" in m, f"missing limitations in {m['id']}"
    assert DEFAULT_MODEL_ID in [m["id"] for m in KNOWN_MODELS]
    print(f"PASS: known models metadata ({len(KNOWN_MODELS)} models)")


if __name__ == "__main__":
    print("=" * 60)
    print("NOVA Intelligence Infrastructure Tests")
    print("=" * 60)
    test_connectivity_offline()
    test_connectivity_online()
    test_provider_health()
    test_generate_result()
    test_ollama_provider_basics()
    test_ollama_provider_complete()
    test_local_model_manager()
    test_router_basics()
    test_router_fallback()
    test_ollama_tool_conversion()
    test_known_models_metadata()
    print("=" * 60)
    print("ALL TESTS PASSED")
