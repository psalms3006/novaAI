"""Final comprehensive test for nova_intelligence."""
import logging
logging.basicConfig(level=logging.WARNING)
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from nova_intelligence.connectivity import ConnectivityManager, ConnectivityState
from nova_intelligence.provider import GenerateResult, ProviderCapability, ProviderHealth
from nova_intelligence.ollama_provider import OllamaProvider
from nova_intelligence.local_model_manager import LocalModelManager, KNOWN_MODELS, DEFAULT_MODEL_ID
from nova_intelligence.router import init_router
from nova_intelligence.gemini_provider import GeminiProvider

# 1. Connectivity
mgr = ConnectivityManager(dns_hosts=("192.0.2.1",), dns_timeout=1.0, check_interval=999)
assert mgr.check_now() == ConnectivityState.OFFLINE
print("1. connectivity OFFLINE: PASS")

# 2. Provider health
h = ProviderHealth()
h.record_success(); h.record_failure("t"); h.record_failure("t2"); h.record_failure("t3")
assert not h.is_healthy
print("2. provider health: PASS")

# 3. Generate result
r = GenerateResult(text="hi"); assert r.ok
r2 = GenerateResult(error="fail"); assert not r2.ok
print("3. generate result: PASS")

# 4. Ollama provider
p = OllamaProvider(model="tinyllama")
assert p.name == "ollama:tinyllama"
running = p.is_running()
print("4. ollama provider: PASS (running=%s)" % running)

# 5. Local model manager
mm = LocalModelManager()
avail = mm.list_available()
assert len(avail) >= 4
rec = mm.get_recommended()
assert rec["recommended"]
print("5. local model manager: PASS (%d models, recommended=%s)" % (len(avail), rec["id"]))

# 6. Router
conn = ConnectivityManager(dns_timeout=1.0, check_interval=999)
conn._state = ConnectivityState.OFFLINE
ollama = OllamaProvider(model="tinyllama")
gemini = GeminiProvider(api_key="")
router = init_router(gemini_provider=gemini, ollama_provider=ollama, connectivity=conn)
provider = router.select_provider()
if running and p.is_available():
    assert provider is ollama
    print("6. router offline -> ollama: PASS")
else:
    assert provider is None
    print("6. router offline -> None (no ollama): PASS")

# 7. Tool conversion
tools = [{"function_declarations": [
    {"name": "web_search", "description": "Search", "parameters": {}},
    {"name": "read_file", "description": "Read", "parameters": {}},
]}]
converted = p._convert_tools(tools)
assert len(converted) == 2
assert converted[0]["function"]["name"] == "web_search"
print("7. tool conversion: PASS")

# 8. Known models metadata
for m in KNOWN_MODELS:
    assert "id" in m and "size_gb" in m and "tool_calling" in m and "strengths" in m
assert DEFAULT_MODEL_ID in [m["id"] for m in KNOWN_MODELS]
print("8. known models: PASS (%d validated)" % len(KNOWN_MODELS))

# 9. Gemini provider
g = GeminiProvider(api_key="")
assert g.name == "gemini"
assert not g.is_available()
print("9. gemini provider: PASS")

print("\nALL 9 TESTS PASSED")
