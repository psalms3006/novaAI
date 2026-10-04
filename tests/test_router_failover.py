"""Regression tests for IntelligenceRouter provider selection and failover.

These pin the behaviour that was missing when NOVA's only cloud provider broke:
the router picked one provider, handed its error straight to the user, and
never tried the local model — so "graceful fallback" existed on paper only.
"""
from __future__ import annotations

import pytest

from nova_intelligence.connectivity import ConnectivityManager, ConnectivityState
from nova_intelligence.provider import (
    GenerateResult, ProviderCapability, ProviderHealth,
)
from nova_intelligence.router import IntelligenceRouter


class FakeProvider:
    """Configurable stand-in for a real provider."""

    def __init__(
        self,
        name: str,
        *,
        available: bool = True,
        text: str = "ok",
        error: str = "",
        raises: Exception | None = None,
        stream_chunks: list[str] | None = None,
        stream_raises: Exception | None = None,
        capabilities: ProviderCapability | None = None,
    ):
        self._name = name
        self._available = available
        self._text = text
        self._error = error
        self._raises = raises
        self._stream_chunks = stream_chunks
        self._stream_raises = stream_raises
        self._health = ProviderHealth()
        self._capabilities = capabilities or (
            ProviderCapability.CHAT
            | ProviderCapability.TOOL_CALLING
            | ProviderCapability.STREAMING
        )
        self.complete_calls = 0
        self.stream_calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> ProviderCapability:
        return self._capabilities

    @property
    def health(self) -> ProviderHealth:
        return self._health

    def is_available(self) -> bool:
        return self._available

    def complete(self, messages, system="", tools=None, temperature=0.7, max_tokens=1024):
        self.complete_calls += 1
        if self._raises:
            raise self._raises
        if self._error:
            return GenerateResult(error=self._error, provider=self._name)
        return GenerateResult(text=self._text, provider=self._name)

    def stream(self, messages, system="", tools=None, temperature=0.7, max_tokens=1024):
        self.stream_calls += 1
        if self._stream_raises:
            raise self._stream_raises
        for c in (self._stream_chunks if self._stream_chunks is not None else [self._text]):
            yield c

    def get_model_info(self) -> dict:
        return {"provider": self._name}


class FixedConnectivity(ConnectivityManager):
    def __init__(self, state: ConnectivityState):
        super().__init__()
        self._forced = state

    @property
    def state(self) -> ConnectivityState:
        return self._forced


def _router(state=ConnectivityState.ONLINE, **providers) -> IntelligenceRouter:
    r = IntelligenceRouter(connectivity=FixedConnectivity(state))
    for name, prov in providers.items():
        r.register_provider(name, prov)
    return r


MSGS = [{"role": "user", "content": "hi"}]


# ── complete() failover ───────────────────────────────────────────────────────

def test_complete_falls_over_when_primary_returns_error():
    gemini = FakeProvider("gemini", error="CERTIFICATE_VERIFY_FAILED")
    ollama = FakeProvider("ollama", text="local answer")
    r = _router(gemini=gemini, ollama=ollama)

    result = r.complete(MSGS)

    assert result.error == ""
    assert result.text == "local answer"
    assert gemini.complete_calls == 1
    assert ollama.complete_calls == 1


def test_complete_falls_over_when_primary_raises():
    gemini = FakeProvider("gemini", raises=RuntimeError("boom"))
    ollama = FakeProvider("ollama", text="local answer")
    r = _router(gemini=gemini, ollama=ollama)

    assert r.complete(MSGS).text == "local answer"
    # The raising provider must be marked unhealthy so ranking learns from it.
    assert gemini.health.consecutive_fails == 1


def test_complete_reports_all_errors_when_every_provider_fails():
    gemini = FakeProvider("gemini", error="401 bad key")
    ollama = FakeProvider("ollama", error="connection refused")
    r = _router(gemini=gemini, ollama=ollama)

    result = r.complete(MSGS)

    assert result.text == ""
    assert "401 bad key" in result.error
    assert "connection refused" in result.error


def test_complete_reports_clearly_when_no_provider_is_available():
    r = _router(gemini=FakeProvider("gemini", available=False))
    result = r.complete(MSGS)
    assert "No intelligence provider available" in result.error


def test_unavailable_provider_is_skipped_entirely():
    gemini = FakeProvider("gemini", available=False)
    ollama = FakeProvider("ollama", text="local answer")
    r = _router(gemini=gemini, ollama=ollama)

    assert r.complete(MSGS).text == "local answer"
    assert gemini.complete_calls == 0


# ── ranking ───────────────────────────────────────────────────────────────────

def test_offline_state_ranks_local_provider_first():
    gemini = FakeProvider("gemini")
    ollama = FakeProvider("ollama")
    r = _router(state=ConnectivityState.OFFLINE, gemini=gemini, ollama=ollama)

    assert [p.name for p in r.rank_providers()] == ["ollama", "gemini"]


def test_online_state_ranks_cloud_provider_first():
    r = _router(gemini=FakeProvider("gemini"), ollama=FakeProvider("ollama"))
    assert [p.name for p in r.rank_providers()] == ["gemini", "ollama"]


def test_online_preferred_false_ranks_local_first():
    r = _router(gemini=FakeProvider("gemini"), ollama=FakeProvider("ollama"))
    r.set_online_preferred(False)
    assert [p.name for p in r.rank_providers()] == ["ollama", "gemini"]


def test_unhealthy_provider_ranks_last_but_is_still_offered():
    gemini = FakeProvider("gemini")
    ollama = FakeProvider("ollama")
    for _ in range(3):
        gemini.health.record_failure("nope")
    assert not gemini.health.is_healthy

    r = _router(gemini=gemini, ollama=ollama)
    ranked = [p.name for p in r.rank_providers()]
    assert ranked == ["ollama", "gemini"]


def test_vision_requirement_filters_out_providers_without_vision():
    gemini = FakeProvider(
        "gemini",
        capabilities=ProviderCapability.CHAT | ProviderCapability.VISION,
    )
    ollama = FakeProvider("ollama", capabilities=ProviderCapability.CHAT)
    r = _router(gemini=gemini, ollama=ollama)

    assert [p.name for p in r.rank_providers(require_vision=True)] == ["gemini"]


def test_local_only_tools_route_to_the_local_provider():
    r = _router(gemini=FakeProvider("gemini"), ollama=FakeProvider("ollama"))
    ranked = r.rank_providers(tool_names=["open_app", "file_read"])
    assert ranked[0].name == "ollama"


def test_network_required_tools_route_to_the_cloud_provider():
    r = _router(gemini=FakeProvider("gemini"), ollama=FakeProvider("ollama"))
    ranked = r.rank_providers(tool_names=["web_api_call"])
    assert ranked[0].name == "gemini"


# ── stream() failover ─────────────────────────────────────────────────────────

def test_stream_falls_over_before_any_chunk_is_emitted():
    gemini = FakeProvider("gemini", stream_raises=RuntimeError("tls broken"))
    ollama = FakeProvider("ollama", stream_chunks=["local ", "answer"])
    r = _router(gemini=gemini, ollama=ollama)

    assert "".join(r.stream(MSGS)) == "local answer"


def test_stream_does_not_switch_providers_after_emitting_text():
    """A mid-stream failure must not splice two different answers together."""
    class Flaky(FakeProvider):
        def stream(self, messages, system="", tools=None, temperature=0.7, max_tokens=1024):
            self.stream_calls += 1
            yield "partial "
            raise RuntimeError("dropped")

    gemini = Flaky("gemini")
    ollama = FakeProvider("ollama", stream_chunks=["local answer"])
    r = _router(gemini=gemini, ollama=ollama)

    out = "".join(r.stream(MSGS))
    assert out.startswith("partial ")
    assert "Response interrupted" in out
    assert "local answer" not in out
    assert ollama.stream_calls == 0


def test_empty_stream_retries_same_provider_non_streamed_before_failing_over():
    """An empty stream usually means a non-text part, not a provider outage."""
    gemini = FakeProvider("gemini", stream_chunks=[], text="recovered")
    ollama = FakeProvider("ollama", stream_chunks=["local answer"])
    r = _router(gemini=gemini, ollama=ollama)

    assert "".join(r.stream(MSGS)) == "recovered"
    assert gemini.complete_calls == 1
    assert ollama.stream_calls == 0


def test_stream_raises_when_every_provider_fails():
    gemini = FakeProvider("gemini", stream_raises=RuntimeError("a"))
    ollama = FakeProvider("ollama", stream_raises=RuntimeError("b"))
    r = _router(gemini=gemini, ollama=ollama)

    # Must raise, not yield a bracketed diagnostic — callers have to be able to
    # tell a failure from an answer, and the user must never see raw error text
    # rendered as NOVA's reply.
    with pytest.raises(RuntimeError) as exc:
        list(r.stream(MSGS))
    assert "a" in str(exc.value) and "b" in str(exc.value)


def test_provider_switch_callback_fires_on_failover():
    switches = []
    gemini = FakeProvider("gemini", error="down")
    ollama = FakeProvider("ollama", text="local")
    r = _router(gemini=gemini, ollama=ollama)
    r.on_provider_switch(lambda old, new: switches.append((old, new)))

    r.complete(MSGS)

    assert ("gemini", "ollama") in switches


# ── provider-agnostic local/remote classification ─────────────────────────────

def test_provider_is_local_uses_the_declared_attribute():
    from nova_intelligence.provider import provider_is_local

    class Declared:
        is_local = True
        name = "totally-cloud-sounding"

    class DeclaredRemote:
        is_local = False
        name = "ollama-sounding-but-remote"

    assert provider_is_local(Declared()) is True
    assert provider_is_local(DeclaredRemote()) is False


def test_provider_is_local_falls_back_to_the_name_heuristic():
    from nova_intelligence.provider import provider_is_local

    class Legacy:
        name = "ollama:mistral"

    class LegacyCloud:
        name = "anthropic"

    assert provider_is_local(Legacy()) is True
    assert provider_is_local(LegacyCloud()) is False


def test_real_providers_declare_their_locality():
    from nova_intelligence.gemini_provider import GeminiProvider
    from nova_intelligence.ollama_provider import OllamaProvider
    assert OllamaProvider.is_local is True
    assert GeminiProvider.is_local is False


def test_ranking_does_not_depend_on_a_provider_being_named_gemini():
    """A second cloud provider must rank as remote, not accidentally as local."""
    cloud = FakeProvider("anthropic")
    cloud.is_local = False
    local = FakeProvider("ollama")
    local.is_local = True

    r = _router(anthropic=cloud, ollama=local)
    assert [p.name for p in r.rank_providers()] == ["anthropic", "ollama"]

    r_off = _router(state=ConnectivityState.OFFLINE, anthropic=cloud, ollama=local)
    assert [p.name for p in r_off.rank_providers()] == ["ollama", "anthropic"]


# ── two-tier local fallback (Qwen primary, TinyLlama secondary) ────────────────
#
# nova.py registers a second OllamaProvider under the key "ollama-fallback"
# once it starts up so a Qwen failure (not installed, OOM, crashed) does not
# read as "no intelligence provider available" when TinyLlama is sitting
# right there on disk. No router change was needed for this -- rank_providers
# and complete()/stream() were already generic over however many providers
# are registered. These tests pin that the plain registration is enough.

def test_falls_through_gemini_then_qwen_to_tinyllama():
    gemini = FakeProvider("gemini", raises=RuntimeError("no network"))
    qwen = FakeProvider("ollama", raises=RuntimeError("model not installed"))
    tinyllama = FakeProvider("ollama-fallback", text="tinyllama answer")
    r = _router(gemini=gemini, ollama=qwen, **{"ollama-fallback": tinyllama})

    result = r.complete(MSGS)

    assert result.text == "tinyllama answer"
    assert gemini.complete_calls == 1
    assert qwen.complete_calls == 1
    assert tinyllama.complete_calls == 1


def test_tinyllama_only_tried_after_qwen_fails():
    qwen = FakeProvider("ollama", text="qwen answer")
    tinyllama = FakeProvider("ollama-fallback", text="tinyllama answer")
    r = _router(state=ConnectivityState.OFFLINE, ollama=qwen,
                **{"ollama-fallback": tinyllama})

    assert r.complete(MSGS).text == "qwen answer"
    assert tinyllama.complete_calls == 0


def test_local_fallback_ranks_after_the_primary_local_model():
    qwen = FakeProvider("ollama")
    tinyllama = FakeProvider("ollama-fallback")
    r = _router(state=ConnectivityState.OFFLINE, ollama=qwen,
                **{"ollama-fallback": tinyllama})

    assert [p.name for p in r.rank_providers()] == ["ollama", "ollama-fallback"]
