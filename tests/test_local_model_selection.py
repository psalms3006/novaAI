"""Regression tests for local model resolution.

The bug: the configured local model name was used unchecked. When it was not
installed — the shipped default (qwen2.5:3b) on a machine that has something
else, or a model the user removed — OllamaProvider.is_available() returned
False, the router dropped the local provider, and NOVA reported that no
intelligence provider was available while usable models sat on disk.
"""
from __future__ import annotations

import pytest

from nova_intelligence.local_model_manager import (
    DEFAULT_MODEL_ID, KNOWN_MODELS, LocalModelManager,
)


@pytest.fixture
def manager(monkeypatch):
    return LocalModelManager()


def _fake_installed(monkeypatch, names):
    class _FakeProvider:
        def __init__(self, *a, **kw):
            pass

        def list_models(self):
            return [{"name": n} for n in names]

    monkeypatch.setattr(
        "nova_intelligence.local_model_manager.OllamaProvider", _FakeProvider
    )


def _configured(monkeypatch, manager, value):
    monkeypatch.setattr(manager, "_configured_model", lambda: value)


def test_configured_model_wins_when_installed(manager, monkeypatch):
    _fake_installed(monkeypatch, ["qwen2.5:1.5b", "mistral:latest"])
    _configured(monkeypatch, manager, "qwen2.5:1.5b")
    assert manager._load_current_model() == "qwen2.5:1.5b"


def test_uninstalled_configured_model_falls_back_to_an_installed_one(manager, monkeypatch):
    _fake_installed(monkeypatch, ["mistral:latest", "tinyllama:latest"])
    _configured(monkeypatch, manager, "qwen2.5:3b")
    assert manager._load_current_model() == "mistral:latest"


def test_fallback_prefers_a_tool_calling_model_over_a_faster_one(manager, monkeypatch):
    # tinyllama is smaller and faster but cannot call tools, which is NOVA's
    # core loop — it must never be preferred over a tool-calling model.
    _fake_installed(monkeypatch, ["tinyllama:latest", "gemma2:2b"])
    _configured(monkeypatch, manager, "not-installed:1b")
    assert manager._load_current_model() == "gemma2:2b"


def test_fallback_prefers_stronger_reasoning(manager, monkeypatch):
    _fake_installed(monkeypatch, ["gemma2:2b", "mistral:latest"])
    _configured(monkeypatch, manager, "not-installed:1b")
    assert manager._load_current_model() == "mistral:latest"


def test_configured_model_is_kept_when_nothing_is_installed(manager, monkeypatch):
    _fake_installed(monkeypatch, [])
    _configured(monkeypatch, manager, "qwen2.5:3b")
    assert manager._load_current_model() == "qwen2.5:3b"


def test_configured_model_is_kept_when_ollama_is_unreachable(manager, monkeypatch):
    class _Boom:
        def __init__(self, *a, **kw):
            pass

        def list_models(self):
            raise ConnectionError("ollama down")

    monkeypatch.setattr(
        "nova_intelligence.local_model_manager.OllamaProvider", _Boom
    )
    _configured(monkeypatch, manager, "qwen2.5:3b")
    assert manager._load_current_model() == "qwen2.5:3b"


def test_tinyllama_is_used_only_as_a_last_resort(manager, monkeypatch):
    _fake_installed(monkeypatch, ["tinyllama:latest"])
    _configured(monkeypatch, manager, "not-installed:1b")
    assert manager._load_current_model() == "tinyllama:latest"


def test_unknown_installed_model_is_still_usable(manager, monkeypatch):
    _fake_installed(monkeypatch, ["some-new-model:7b"])
    _configured(monkeypatch, manager, "not-installed:1b")
    assert manager._load_current_model() == "some-new-model:7b"


def test_default_model_id_is_present_in_the_catalog():
    assert any(m["id"] == DEFAULT_MODEL_ID for m in KNOWN_MODELS)
