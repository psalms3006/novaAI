"""Quick test for nova_intelligence package."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))

from nova_intelligence.connectivity import ConnectivityManager, ConnectivityState
from nova_intelligence.provider import GenerateResult, ProviderCapability, ProviderHealth
from nova_intelligence.ollama_provider import OllamaProvider
from nova_intelligence.local_model_manager import LocalModelManager, KNOWN_MODELS, DEFAULT_MODEL_ID
from nova_intelligence.router import IntelligenceRouter, init_router
from nova_intelligence.gemini_provider import GeminiProvider

print("1. connectivity offline")
mgr = ConnectivityManager(dns_hosts=("192.0.2.1",), dns_timeout=1.0, check_interval=999)
state = mgr.check_now()
assert state == ConnectivityState.OFFLINE, f"expected OFFLINE, got {state}"
print("   PASS")

print("2. provider health")
h = ProviderHealth()
assert h.is_healthy
h.record_success(0.1)
h.record_failure("test")
assert h.consecutive_fails == 1
assert h.is_healthy
h.record_failure("t2")
h.record_failure("t3")
assert not h.is_healthy
print("   PASS")

print("3. generate result")
r = GenerateResult(text="hello", provider="test")
assert r.ok
r2 = GenerateResult(error="fail")
assert not r2.ok
print("   PASS")

print("4. ollama provider")
p = OllamaProvider(model="tinyllama")
assert p.name == "ollama:tinyllama"
info = p.get_model_info()
assert info["provider"] == "ollama"
running = p.is_running()
print(f"   Ollama running={running}")

print("5. local model manager")
m = LocalModelManager()
available = m.list_available()
assert len(available) >= 4
rec = m.get_recommended()
assert rec is not None and rec["recommended"]
print(f"   {len(available)} models, recommended: {rec['id']}")

print("6. router")
conn = ConnectivityManager(dns_timeout=1.0, check_interval=999)
conn._state = ConnectivityState.OFFLINE
ollama = OllamaProvider(model="tinyllama")
gemini = GeminiProvider(api_key="")
router = init_router(gemini_provider=gemini, ollama_provider=ollama, connectivity=conn)
assert router.get_provider("ollama") is ollama
provider = router.select_provider()
if ollama.is_available():
    assert provider is ollama
    print("   selected ollama (expected)")
else:
    assert provider is None
    print("   no provider (expected if ollama not installed)")

print("7. tool conversion")
gemini_tools = [{"function_declarations": [
    {"name": "web_search", "description": "Search", "parameters": {}},
    {"name": "read_file", "description": "Read", "parameters": {}},
]}]
ollama_tools = p._convert_tools(gemini_tools)
assert len(ollama_tools) == 2
assert ollama_tools[0]["function"]["name"] == "web_search"
print("   PASS")

print("8. known models metadata")
for m in KNOWN_MODELS:
    assert "id" in m and "size_gb" in m and "tool_calling" in m
assert DEFAULT_MODEL_ID in [m["id"] for m in KNOWN_MODELS]
print(f"   {len(KNOWN_MODELS)} models validated")

print("\nALL TESTS PASSED")
