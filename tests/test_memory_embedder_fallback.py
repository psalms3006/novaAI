"""Personal memory search must work in the packaged app.

The EXE excludes torch, so sentence-transformers can never import there, and
every packaged run logged "sentence-transformers not installed -- memory search
disabled". The same model is already shipped to users as ONNX for document
search; memory now falls back to it through an adapter with the one method
memory_extra calls (``encode(texts, normalize_embeddings=True)``).
"""
import builtins

import numpy as np
import pytest

from nova_core.rag import embeddings
from nova_core.rag.embeddings import OnnxBackend, OnnxSentenceEncoder, load_memory_encoder


def _onnx_ready():
    ok, _ = OnnxBackend().available()
    return ok


needs_onnx = pytest.mark.skipif(not _onnx_ready(), reason="local ONNX model not downloaded")


class _FakeBackend:
    dim = 3

    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def available(self):
        return (self.ok, "fake" if self.ok else "not downloaded")

    def embed(self, texts):
        self.calls.append(list(texts))
        return np.ones((len(texts), 3), dtype=np.float32) / np.sqrt(3)


def test_encoder_has_the_sentence_transformers_shape():
    enc = OnnxSentenceEncoder(_FakeBackend())
    out = enc.encode(["a", "b"], normalize_embeddings=True)
    assert out.shape == (2, 3)
    assert out.dtype == np.float32
    assert enc.get_sentence_embedding_dimension() == 3


def test_encoder_accepts_a_single_string():
    enc = OnnxSentenceEncoder(_FakeBackend())
    assert enc.encode("hello").shape == (1, 3)


def _block_sentence_transformers(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "sentence_transformers" or name.startswith("sentence_transformers."):
            raise ImportError("No module named 'sentence_transformers'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_falls_back_to_onnx_when_sentence_transformers_is_missing(monkeypatch):
    _block_sentence_transformers(monkeypatch)
    monkeypatch.setattr(embeddings, "OnnxBackend", lambda *a, **k: _FakeBackend(ok=True))
    enc, how = load_memory_encoder("all-MiniLM-L6-v2")
    assert isinstance(enc, OnnxSentenceEncoder)
    assert "onnx" in how


def test_reports_why_when_neither_backend_is_usable(monkeypatch):
    _block_sentence_transformers(monkeypatch)
    monkeypatch.setattr(embeddings, "OnnxBackend", lambda *a, **k: _FakeBackend(ok=False))
    enc, how = load_memory_encoder("all-MiniLM-L6-v2")
    assert enc is None
    assert "sentence-transformers" in how and "not downloaded" in how


@needs_onnx
def test_real_onnx_encoder_ranks_related_text_first():
    enc = OnnxSentenceEncoder(OnnxBackend())
    facts = ["The user's dog is called Biscuit.", "The user works as a nurse."]
    idx = enc.encode(facts, normalize_embeddings=True)
    q = enc.encode(["what is my pet's name"], normalize_embeddings=True)
    scores = idx @ q[0]
    assert scores[0] > scores[1]
    assert np.allclose(np.linalg.norm(idx, axis=1), 1.0, atol=1e-4)


@needs_onnx
def test_onnx_matches_sentence_transformers_so_old_indexes_stay_valid():
    st = pytest.importorskip("sentence_transformers")
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "nova_embedder"
    if not src.exists():
        pytest.skip("nova_embedder not present")
    texts = ["remind me to call my sister", "the capital of France is Paris"]
    a = st.SentenceTransformer(str(src)).encode(texts, normalize_embeddings=True)
    b = OnnxSentenceEncoder(OnnxBackend()).encode(texts, normalize_embeddings=True)
    assert np.allclose(a, b, atol=1e-3)
