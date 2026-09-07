"""Regression tests for NOVA's semantic fact memory.

Three real, compounding bugs are covered here:

  1. ``add_memory_fact`` returned early whenever faiss-cpu was missing, so every
     "remember this" stored nothing while the UI still reported memory as
     enabled. FAISS is not installed in the packaged app.
  2. ``_memory_lock`` was a plain ``threading.Lock`` while ``add_memory_fact``
     held it and then called ``_atomic_save_memory``, which takes it again —
     a self-deadlock that was masked by bug 1.
  3. Retrieval went through a FAISS-only path, so stored facts never reached
     the prompt even when they had been saved.

The index used here is the numpy fallback, which is what ships.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import numpy as np
import pytest

# memory_extra imports nova, and nova imports memory_extra — the cycle only
# resolves if nova is the one that gets imported first.
import nova  # noqa: F401  (import order matters)
import memory_extra
import nova_state


# ── the numpy index itself ────────────────────────────────────────────────────

def _unit(vec):
    v = np.asarray(vec, dtype=np.float32)
    return v / np.linalg.norm(v)


def test_numpy_index_starts_empty():
    idx = memory_extra._NumpyFlatIP(4)
    assert idx.ntotal == 0
    scores, indices = idx.search(np.zeros((1, 4), dtype=np.float32), 3)
    assert scores.shape[1] == 0 and indices.shape[1] == 0


def test_numpy_index_ranks_by_inner_product():
    idx = memory_extra._NumpyFlatIP(3)
    idx.add(np.array([_unit([1, 0, 0]), _unit([0, 1, 0]), _unit([1, 1, 0])], dtype=np.float32))
    assert idx.ntotal == 3

    scores, indices = idx.search(_unit([1, 0, 0]).reshape(1, 3), 3)
    assert indices[0][0] == 0                     # exact match ranks first
    assert scores[0][0] == pytest.approx(1.0, abs=1e-5)
    assert scores[0][0] >= scores[0][1] >= scores[0][2]


def test_numpy_index_k_is_clamped_to_size():
    idx = memory_extra._NumpyFlatIP(2)
    idx.add(np.array([_unit([1, 0])], dtype=np.float32))
    scores, indices = idx.search(_unit([1, 0]).reshape(1, 2), 10)
    assert scores.shape == (1, 1) and indices.shape == (1, 1)


def test_numpy_index_rejects_wrong_dimension():
    idx = memory_extra._NumpyFlatIP(3)
    with pytest.raises(ValueError):
        idx.add(np.zeros((1, 5), dtype=np.float32))


def test_numpy_index_accepts_a_1d_vector():
    idx = memory_extra._NumpyFlatIP(3)
    idx.add(_unit([1, 0, 0]))
    assert idx.ntotal == 1


def test_new_index_is_usable_without_faiss(monkeypatch):
    monkeypatch.setattr(memory_extra, "HAS_FAISS", False)
    idx = memory_extra._new_index()
    assert isinstance(idx, memory_extra._NumpyFlatIP)
    assert idx.d == memory_extra.DIMENSION


# ── store / retrieve with a deterministic fake embedder ───────────────────────

class _FakeEmbedder:
    """Deterministic hashed bag-of-words embedder — no model download."""

    def encode(self, texts, normalize_embeddings=True, **kw):
        if isinstance(texts, str):
            texts = [texts]
        out = np.zeros((len(texts), memory_extra.DIMENSION), dtype=np.float32)
        for r, t in enumerate(texts):
            for word in str(t).lower().split():
                out[r, hash(word) % memory_extra.DIMENSION] += 1.0
            n = np.linalg.norm(out[r])
            if n and normalize_embeddings:
                out[r] /= n
        return out


@pytest.fixture
def memory_env(tmp_path, monkeypatch):
    """Isolated memory files + a fake embedder. Never touches the user's data."""
    monkeypatch.setattr(memory_extra, "HAS_FAISS", False)
    monkeypatch.setattr(memory_extra, "MEMORY_META_FILE", tmp_path / "meta.json")
    monkeypatch.setattr(memory_extra, "MEMORY_TEXTS_FILE", tmp_path / "texts.json")
    monkeypatch.setattr(memory_extra, "MEMORY_INDEX_FILE", tmp_path / "m.index")
    monkeypatch.setattr(memory_extra, "_memory_lock", threading.RLock())
    monkeypatch.setattr(nova_state, "_embedder", _FakeEmbedder())
    monkeypatch.setattr(nova_state, "_memory_texts", [])
    monkeypatch.setattr(nova_state, "_living_memory", None, raising=False)
    memory_extra._rebuild_index()
    return tmp_path


def test_fact_is_stored_without_faiss(memory_env):
    assert memory_extra.add_memory_fact("User's favourite language is Rust", {}) is True
    assert nova_state._memory_texts == ["User's favourite language is Rust"]


def test_stored_fact_is_persisted_to_disk(memory_env):
    memory_extra.add_memory_fact("User lives in Lagos", {"user_name": "t"})
    saved = json.loads(Path(memory_extra.MEMORY_TEXTS_FILE).read_text(encoding="utf-8"))
    assert saved == ["User lives in Lagos"]


def test_add_memory_fact_does_not_deadlock(memory_env):
    """add_memory_fact -> _atomic_save_memory re-enters the same lock."""
    done = threading.Event()
    err = []

    def _run():
        try:
            memory_extra.add_memory_fact("a durable fact", {})
        except Exception as e:  # pragma: no cover
            err.append(e)
        finally:
            done.set()

    threading.Thread(target=_run, daemon=True).start()
    assert done.wait(timeout=10), "add_memory_fact deadlocked"
    assert not err


def test_exact_duplicate_is_rejected(memory_env):
    assert memory_extra.add_memory_fact("the sky is blue", {}) is True
    assert memory_extra.add_memory_fact("the sky is blue", {}) is False
    assert len(nova_state._memory_texts) == 1


def test_empty_fact_is_rejected(memory_env):
    assert memory_extra.add_memory_fact("   ", {}) is False
    assert memory_extra.add_memory_fact("", {}) is False
    assert nova_state._memory_texts == []


def test_search_retrieves_the_relevant_fact(memory_env):
    memory_extra.add_memory_fact("User's favourite language is Rust", {})
    memory_extra.add_memory_fact("User lives in Lagos", {})
    assert memory_extra.search_memory("favourite language") == [
        "User's favourite language is Rust"
    ]


def test_search_is_empty_when_nothing_is_stored(memory_env):
    assert memory_extra.search_memory("anything") == []


def test_memory_survives_a_reload(memory_env):
    memory_extra.add_memory_fact("User's favourite language is Rust", {})
    memory_extra.add_memory_fact("User lives in Lagos", {})

    nova_state._memory_texts = []
    memory_extra.load_memory()

    assert len(nova_state._memory_texts) == 2
    assert memory_extra.search_memory("favourite language") == [
        "User's favourite language is Rust"
    ]


def test_facts_are_kept_even_with_no_embedder(memory_env, monkeypatch):
    """A missing embedder must not silently discard what the user said."""
    monkeypatch.setattr(nova_state, "_embedder", None)
    assert memory_extra.add_memory_fact("remember the milk", {}) is True
    assert "remember the milk" in nova_state._memory_texts


def test_index_stays_aligned_with_texts_when_embedder_is_missing(memory_env, monkeypatch):
    """Row i of the index must map to _memory_texts[i], or search returns lies."""
    memory_extra.add_memory_fact("User lives in Lagos", {})
    monkeypatch.setattr(nova_state, "_embedder", None)
    memory_extra.add_memory_fact("an unembeddable fact", {})
    monkeypatch.setattr(nova_state, "_embedder", _FakeEmbedder())

    assert memory_extra._faiss_index.ntotal == len(nova_state._memory_texts)
    # The real fact is still retrievable and not mislabelled.
    assert memory_extra.search_memory("Lagos") == ["User lives in Lagos"]


def test_build_memory_context_includes_a_matching_fact(memory_env):
    memory_extra.add_memory_fact("User's favourite language is Rust", {})
    ctx = memory_extra.build_memory_context({"user_name": "tester"}, query="favourite language")
    assert "tester" in ctx
    assert "Rust" in ctx


def test_build_memory_context_is_empty_with_no_data(memory_env):
    assert memory_extra.build_memory_context({}, query="anything") == ""
