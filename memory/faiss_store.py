"""
NOVA FAISS Memory Store — Semantic search via FAISS (preserved from original NOVA).

This module wraps the original FAISS-based semantic memory into a clean class
that integrates with the new memory subsystem.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from core.utils import atomic_json_write, atomic_json_read

log = logging.getLogger("nova.memory.faiss")

try:
    import faiss
    HAS_FAISS = True
except ImportError:
    HAS_FAISS = False
    log.warning("FAISS not installed. Semantic search disabled.")


class NovaFAISSMemory:
    """
    FAISS-based semantic memory store with thread-safe operations.

    Features:
      - Inner-product similarity search
      - Duplicate detection (exact match + similarity threshold)
      - Atomic persistence (temp file + os.replace)
      - Auto-index rebuilding on mismatch
    """

    def __init__(
        self,
        dimension: int = 384,
        top_k: int = 5,
        min_score: float = 0.30,
        meta_file: Optional[Path] = None,
        texts_file: Optional[Path] = None,
        index_file: Optional[Path] = None,
        embed_model: str = "all-MiniLM-L6-v2",
    ) -> None:
        self.dimension = dimension
        self.top_k = top_k
        self.min_score = min_score
        self.meta_file = meta_file or Path("memory_meta.json")
        self.texts_file = texts_file or Path("memory_texts.json")
        self.index_file = index_file or Path("memory.index")
        self.embed_model = embed_model

        self._texts: List[str] = []
        self._meta: Dict[str, str] = {"user_name": "", "user_gender": ""}
        self._index: Optional[Any] = None
        self._embedder: Optional[Any] = None
        self._lock = threading.Lock()
        self._embedder_lock = threading.Lock()

    def initialize(self) -> Dict[str, str]:
        """Load metadata, texts, and FAISS index. Returns meta dict."""
        self._load_meta()
        self._load_texts()
        self._load_index()
        log.info(f"FAISS memory initialized — {len(self._texts)} facts, embedder={'loaded' if self._embedder else 'lazy'}.")
        return self._meta

    def _load_embedder(self) -> None:
        """Lazily load the sentence transformer embedder."""
        with self._embedder_lock:
            if self._embedder is not None:
                return
            try:
                from sentence_transformers import SentenceTransformer
                local_path = "./nova_embedder"
                import os
                if os.path.isdir(local_path):
                    self._embedder = SentenceTransformer(local_path)
                else:
                    self._embedder = SentenceTransformer(self.embed_model)
                log.info("Sentence transformer embedder loaded.")
            except ImportError:
                log.warning("sentence-transformers not installed. Memory search will return empty results.")
            except Exception as e:
                log.error(f"Failed to load embedder: {e}")

    def _embed(self, text: str) -> np.ndarray:
        if self._embedder is None:
            self._load_embedder()
        if self._embedder is None:
            return np.zeros((1, self.dimension), dtype=np.float32)
        return self._embedder.encode([text], normalize_embeddings=True).astype(np.float32)

    def _load_meta(self) -> None:
        data = atomic_json_read(self.meta_file, default={"user_name": "", "user_gender": ""})
        if isinstance(data, dict):
            self._meta.update({k: str(v) for k, v in data.items() if isinstance(v, str)})

    def _load_texts(self) -> None:
        data = atomic_json_read(self.texts_file, default=[])
        self._texts = data if isinstance(data, list) else []

    def _load_index(self) -> None:
        if not HAS_FAISS:
            return
        self._index = faiss.IndexFlatIP(self.dimension)
        if self._embedder and self.index_file.exists() and self._texts:
            try:
                loaded_idx = faiss.read_index(str(self.index_file))
                if loaded_idx.ntotal == len(self._texts):
                    self._index = loaded_idx
                    log.info(f"FAISS index loaded — {len(self._texts)} facts.")
                else:
                    log.warning("Index/text mismatch. Rebuilding...")
                    self._rebuild_index()
            except Exception as e:
                log.error(f"Corrupted memory.index: {e}. Rebuilding...")
                self._rebuild_index()
        elif self._texts:
            self._rebuild_index()

    def _rebuild_index(self) -> None:
        if not HAS_FAISS:
            return
        self._index = faiss.IndexFlatIP(self.dimension)
        if self._texts and self._embedder:
            embs = self._embedder.encode(self._texts, normalize_embeddings=True).astype(np.float32)
            self._index.add(embs)
            log.info(f"Rebuilt FAISS index — {len(self._texts)} facts.")

    def _save(self) -> None:
        """Atomically save meta, texts, and index."""
        with self._lock:
            atomic_json_write(self.meta_file, self._meta)
            atomic_json_write(self.texts_file, self._texts)
            if HAS_FAISS and self._index is not None:
                import tempfile
                fd, tmp_path = tempfile.mkstemp(suffix=".index")
                try:
                    faiss.write_index(self._index, tmp_path)
                    os.replace(tmp_path, str(self.index_file))
                except BaseException:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise

    def add_fact(self, text: str) -> bool:
        """Add a fact to memory. Returns True if stored, False if duplicate."""
        if self._embedder is None:
            self._load_embedder()
        if not HAS_FAISS or self._embedder is None:
            return False

        text = text.strip()
        if not text:
            return False

        with self._lock:
            # Exact duplicate check
            if text in self._texts:
                return False
            # Similarity duplicate check
            if self._texts and self._index is not None and self._index.ntotal > 0:
                scores, _ = self._index.search(self._embed(text), 1)
                if scores[0][0] > 0.95:
                    return False
            # Add to index and texts
            self._index.add(self._embed(text))
            self._texts.append(text)
            self._save()
            log.info(f"Memory stored: {text[:80]}")
            return True

    def search(self, query: str, top_k: Optional[int] = None) -> List[str]:
        """Search memory for facts similar to query."""
        k = top_k or self.top_k
        if not self._texts or self._index is None or self._index.ntotal == 0:
            return []
        k = min(k, len(self._texts))
        scores, indices = self._index.search(self._embed(query), k)
        return [
            self._texts[i] for i, s in zip(indices[0], scores[0])
            if i < len(self._texts) and s >= self.min_score
        ]

    def build_context(self, query: str = "", max_facts: Optional[int] = None) -> str:
        """Build a memory context string for system prompts."""
        lines = []
        if self._meta.get("user_name"):
            lines.append(f"- User's name is {self._meta['user_name']}")
        if self._meta.get("user_gender"):
            lines.append(f"- User's gender is {self._meta['user_gender']}")

        k = max_facts or self.top_k
        facts = self.search(query) if query else self._texts[-k:]
        for fact in facts:
            lines.append(f"- {fact}")
        return "\n".join(lines) if lines else ""

    def update_meta(self, key: str, value: str) -> None:
        """Update a metadata field (e.g. user_name)."""
        self._meta[key] = value
        with self._lock:
            atomic_json_write(self.meta_file, self._meta)

    @property
    def meta(self) -> Dict[str, str]:
        return dict(self._meta)

    @property
    def fact_count(self) -> int:
        return len(self._texts)

    @property
    def all_facts(self) -> List[str]:
        return list(self._texts)