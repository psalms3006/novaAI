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
    # A machine with plenty of memory, unless a test says otherwise:
    # selection is now RAM-aware, and the tests must not depend on the PC
    # they happen to run on.
    monkeypatch.setattr(
        "nova_intelligence.local_model_manager.total_ram_gb", lambda: 32.0)
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


# -- memory and the configured-but-absent default (2026-10 audit) -----------

def test_a_model_too_big_for_this_machine_loses_to_one_that_fits(manager, monkeypatch):
    """On 8 GB, mistral (recommended 8 GB) made offline replies take minutes
    while qwen2.5:1.5b sat installed."""
    monkeypatch.setattr(
        "nova_intelligence.local_model_manager.total_ram_gb", lambda: 8.0)
    _fake_installed(monkeypatch, ["mistral:latest", "qwen2.5:1.5b", "tinyllama:latest"])
    _configured(monkeypatch, manager, "qwen2.5:3b")
    assert manager._load_current_model() == "qwen2.5:1.5b"


def test_a_fitting_model_beats_a_big_one_even_with_better_reasoning():
    from nova_intelligence.local_model_manager import LocalModelManager
    best = LocalModelManager._best_installed(
        ["mistral:latest", "gemma2:2b"], ram_gb=8.0)
    assert best == "gemma2:2b"


def test_resolve_installed_does_not_need_a_substring_match(monkeypatch):
    """'qwen2.5:3b' is not a substring of 'qwen2.5:1.5b'; that used to make
    the local provider 'unavailable' and send every offline turn to
    tinyllama, which cannot call tools."""
    from nova_intelligence.local_model_manager import resolve_installed
    monkeypatch.setattr(
        "nova_intelligence.local_model_manager.total_ram_gb", lambda: 8.0)
    got = resolve_installed("qwen2.5:3b",
                            ["qwen2.5:1.5b", "tinyllama:latest", "mistral:latest"])
    assert got == "qwen2.5:1.5b"
    assert resolve_installed("mistral", ["mistral:latest"]) == "mistral:latest"
    assert resolve_installed("x", []) is None


def test_the_provider_switches_to_an_installed_model_instead_of_going_dark(monkeypatch):
    from nova_intelligence import ollama_provider as op
    monkeypatch.setattr(
        "nova_intelligence.local_model_manager.total_ram_gb", lambda: 8.0)

    class _R:
        status_code = 200
        def json(self):
            return {"models": [{"name": "qwen2.5:1.5b"}, {"name": "tinyllama:latest"}]}

    monkeypatch.setattr(op.requests, "get", lambda *a, **k: _R())
    p = op.OllamaProvider(model="qwen2.5:3b")
    monkeypatch.setattr(p, "is_running", lambda: True)
    assert p._probe_availability() is True
    assert p.model == "qwen2.5:1.5b"

    last_resort = op.OllamaProvider(model="phi3:mini")
    last_resort.auto_resolve = False
    monkeypatch.setattr(last_resort, "is_running", lambda: True)
    assert last_resort._probe_availability() is False
    assert last_resort.model == "phi3:mini"
