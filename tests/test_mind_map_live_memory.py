"""The memory map shows NOVA's live memory and never loads a model to draw it.

Two faults this pins:
  * memory nodes were read from files beside the source code -- inside the
    bundle once packaged -- so the map disagreed with the Memory page;
  * every map request (and every node click) loaded a SentenceTransformer
    from disk to compute edges.
"""
from __future__ import annotations

import sys
import types

import pytest


class FakeLivingMemory:
    def __init__(self, records):
        self._records = records

    def all_records(self):
        return list(self._records)


class CountingEmbedder:
    def __init__(self):
        self.calls = 0

    def encode(self, texts, show_progress_bar=False):
        self.calls += 1
        # Two clusters: texts mentioning "coffee" and everything else.
        return [[1.0, 0.0] if "coffee" in t else [0.0, 1.0] for t in texts]


@pytest.fixture()
def bridge(monkeypatch):
    from desk import bridge as b
    b._mind_map_edge_cache.update(key=None, edges=None)
    return b


def _records():
    return [
        {"text": "User drinks coffee black", "type": "preference", "importance": 0.9, "updated": 100.0},
        {"text": "User likes coffee at 7am", "type": "habit", "importance": 0.5, "updated": 200.0},
        {"text": "Old name", "type": "identity", "importance": 0.8, "updated": 50.0, "superseded_by": "x"},
    ]


def test_memory_nodes_come_from_living_memory(bridge, monkeypatch):
    monkeypatch.setattr(bridge.nova_state, "_living_memory", FakeLivingMemory(_records()), raising=False)
    monkeypatch.setattr(bridge.nova_state, "_memory_texts", ["Prefers metric units"], raising=False)
    nodes = bridge._collect_mind_map_nodes()
    memory = [n for n in nodes if n["region"] == "memory"]
    texts = [n["detail"] for n in memory]
    assert "User drinks coffee black" in texts and "User likes coffee at 7am" in texts
    assert "Old name" not in texts, "superseded memories must not be drawn"
    assert "Prefers metric units" in texts
    # Forget needs the exact record: text + updated.
    coffee = next(n for n in memory if n["detail"] == "User drinks coffee black")
    assert coffee["updated"] == 100.0


def test_no_living_memory_means_no_memory_nodes(bridge, monkeypatch):
    monkeypatch.setattr(bridge.nova_state, "_living_memory", None, raising=False)
    monkeypatch.setattr(bridge.nova_state, "_memory_texts", [], raising=False)
    nodes = bridge._collect_mind_map_nodes()
    assert not [n for n in nodes if n["region"] == "memory"]


def test_edges_reuse_novas_embedder_and_are_cached(bridge, monkeypatch):
    # Loading a model here is the bug: make any attempt fail loudly.
    monkeypatch.setitem(sys.modules, "sentence_transformers", types.SimpleNamespace(
        SentenceTransformer=lambda *a, **k: (_ for _ in ()).throw(AssertionError("loaded a model"))))
    pytest.importorskip("faiss")
    emb = CountingEmbedder()
    monkeypatch.setattr(bridge.nova_state, "_embedder", emb, raising=False)
    monkeypatch.setattr(bridge.nova_state, "_living_memory", FakeLivingMemory(_records()), raising=False)
    nodes = bridge._collect_mind_map_nodes()

    first = bridge._collect_mind_map_edges(nodes)
    second = bridge._collect_mind_map_edges(nodes)
    assert emb.calls == 1, "a second request with the same nodes must reuse the cached edges"
    assert first == second
    assert any(e["type"] == "semantic" for e in first)


def test_a_node_click_reuses_the_maps_edges(bridge, monkeypatch):
    """The node route rebuilds the map; it must not recompute edges each click."""
    calls = []
    real = bridge._compute_mind_map_edges
    monkeypatch.setattr(bridge, "_compute_mind_map_edges", lambda nodes: calls.append(1) or real(nodes))
    monkeypatch.setattr(bridge.nova_state, "_embedder", None, raising=False)
    monkeypatch.setattr(bridge.nova_state, "_living_memory", FakeLivingMemory(_records()), raising=False)
    nodes = bridge._collect_mind_map_nodes()
    for _ in range(3):
        bridge._collect_mind_map_edges(nodes)
    assert len(calls) == 1


def test_without_an_embedder_edges_are_regional(bridge, monkeypatch):
    monkeypatch.setattr(bridge.nova_state, "_embedder", None, raising=False)
    monkeypatch.setattr(bridge.nova_state, "_living_memory", FakeLivingMemory(_records()), raising=False)
    edges = bridge._collect_mind_map_edges(bridge._collect_mind_map_nodes())
    assert edges and all(e["type"] == "regional" for e in edges)
