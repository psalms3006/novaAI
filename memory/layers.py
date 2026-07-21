"""
NOVA 6-Layer Cognitive Memory — Inspired by VYREN's memory_v2 and Obsidian's knowledge philosophy.

Layers:
  Working     — Current conversation context (volatile, in-memory only)
  Episodic    — Specific past interactions (JSON-persisted)
  Semantic    — General facts and knowledge (JSON-persisted)
  Procedural  — Learned workflows, how-to (JSON-persisted)
  Preference  — User preferences, habits (JSON-persisted)
  Project     — Per-project context (JSON-persisted)

Key features:
  - Importance scoring (0-1) for retrieval prioritization
  - Access count tracking for relevance boosting
  - Memory consolidation: episodic → semantic promotion
  - Contradiction detection across layers
  - Relevance-ranked search with decay
  - Tags and project scoping
  - Memory is DATA, never INSTRUCTIONS (security principle from VYREN)
"""
from __future__ import annotations

import json
import logging
import os
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.utils import atomic_json_write, atomic_json_read

log = logging.getLogger("nova.memory")


class MemoryLayer(Enum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    PREFERENCE = "preference"
    PROJECT = "project"


@dataclass
class MemoryEntry:
    """A single memory record."""
    key: str
    value: str
    layer: MemoryLayer = MemoryLayer.EPISODIC
    importance: float = 0.5
    confidence: float = 1.0
    access_count: int = 0
    tags: List[str] = field(default_factory=list)
    source: str = ""
    project: str = ""
    created: str = field(default_factory=lambda: datetime.now().isoformat())
    last_accessed: str = field(default_factory=lambda: datetime.now().isoformat())
    expires: Optional[str] = None

    def touch(self) -> None:
        """Update access metadata."""
        self.access_count += 1
        self.last_accessed = datetime.now().isoformat()

    def is_expired(self) -> bool:
        if not self.expires:
            return False
        try:
            return datetime.fromisoformat(self.expires) < datetime.now()
        except (ValueError, TypeError):
            return False

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "value": self.value,
            "layer": self.layer.value,
            "importance": self.importance,
            "confidence": self.confidence,
            "access_count": self.access_count,
            "tags": self.tags,
            "source": self.source,
            "project": self.project,
            "created": self.created,
            "last_accessed": self.last_accessed,
            "expires": self.expires,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryEntry":
        d = dict(d)  # Copy
        if isinstance(d.get("layer"), str):
            d["layer"] = MemoryLayer(d["layer"])
        # Handle missing fields from older data
        for f in ("importance", "confidence", "access_count", "tags", "source", "project", "expires"):
            d.setdefault(f, MemoryEntry.__dataclass_fields__[f].default_factory() if callable(MemoryEntry.__dataclass_fields__[f].default_factory) else MemoryEntry.__dataclass_fields__[f].default)
        return cls(**d)


class BaseMemoryStore:
    """Base class for a single memory layer's storage."""

    def __init__(self, layer: MemoryLayer, persist_path: Optional[Path] = None) -> None:
        self.layer = layer
        self._entries: List[MemoryEntry] = []
        self._path = persist_path
        self._lock = threading.Lock()

    def add(self, entry: MemoryEntry) -> None:
        with self._lock:
            # Check for exact duplicate
            for existing in self._entries:
                if existing.key == entry.key and existing.value == entry.value:
                    existing.touch()
                    return
            self._entries.append(entry)
        log.debug(f"[{self.layer.value}] Stored: {entry.key[:60]}")

    def get(self, key: str) -> Optional[MemoryEntry]:
        with self._lock:
            for e in self._entries:
                if e.key == key:
                    e.touch()
                    return e
        return None

    def search(self, query: str, top_k: int = 5) -> List[Tuple[MemoryEntry, float]]:
        """Search by relevance score. Returns [(entry, score)]."""
        query_lower = query.lower()
        results: List[Tuple[MemoryEntry, float]] = []

        with self._lock:
            for entry in self._entries:
                if entry.is_expired():
                    continue
                score = entry.importance
                # Key match bonus
                if query_lower in entry.key.lower():
                    score += 0.3
                # Value match bonus
                if query_lower in entry.value.lower():
                    score += 0.2
                # Tag match bonus
                if any(query_lower in t.lower() for t in entry.tags):
                    score += 0.1
                # Access recency bonus
                try:
                    hours_since = (datetime.now() - datetime.fromisoformat(entry.last_accessed)).total_seconds() / 3600
                    score += max(0, 0.1 - hours_since * 0.001)
                except (ValueError, TypeError):
                    pass
                # Confidence weighting
                score *= entry.confidence
                results.append((entry, score))

        results.sort(key=lambda x: -x[1])
        return results[:top_k]

    def delete(self, key: str) -> bool:
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if e.key != key]
            return len(self._entries) < before

    def all_entries(self) -> List[MemoryEntry]:
        with self._lock:
            return list(self._entries)

    def save(self) -> None:
        if not self._path:
            return
        with self._lock:
            data = [e.to_dict() for e in self._entries]
            atomic_json_write(self._path, data)

    def load(self) -> None:
        if not self._path or not self._path.exists():
            return
        with self._lock:
            data = atomic_json_read(self._path, default=[])
            self._entries = [MemoryEntry.from_dict(d) for d in data if isinstance(d, dict)]
            log.info(f"[{self.layer.value}] Loaded {len(self._entries)} entries.")


class WorkingMemory(BaseMemoryStore):
    """Volatile in-memory store for current conversation context."""
    def __init__(self) -> None:
        super().__init__(MemoryLayer.WORKING, persist_path=None)

    def save(self) -> None:
        pass  # Working memory is never persisted

    def load(self) -> None:
        pass

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class EpisodicMemory(BaseMemoryStore):
    """Specific past interactions."""
    pass


class SemanticMemory(BaseMemoryStore):
    """General facts and knowledge."""
    pass


class ProceduralMemory(BaseMemoryStore):
    """Learned workflows and procedures."""
    pass


class PreferenceMemory(BaseMemoryStore):
    """User preferences and habits."""
    pass


class ProjectMemory(BaseMemoryStore):
    """Per-project context."""
    pass


class NovaCognitiveMemory:
    """
    Unified 6-layer cognitive memory manager.

    Combines all memory layers with:
      - Cross-layer search
      - Memory consolidation (episodic → semantic promotion)
      - Contradiction detection
      - Automatic persistence
      - Context window assembly

    Usage:
        mem = NovaCognitiveMemory(data_dir=Path("nova_memory_layers"))
        mem.initialize()
        mem.add(MemoryEntry(key="user_name", value="Samuel", layer=MemoryLayer.SEMANTIC, importance=0.9))
        results = mem.search("user name")
    """

    def __init__(self, data_dir: Optional[Path] = None) -> None:
        self.data_dir = data_dir or Path("nova_memory_layers")
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.working = WorkingMemory()
        self.episodic = EpisodicMemory(MemoryLayer.EPISODIC, self.data_dir / "episodic.json")
        self.semantic = SemanticMemory(MemoryLayer.SEMANTIC, self.data_dir / "semantic.json")
        self.procedural = ProceduralMemory(MemoryLayer.PROCEDURAL, self.data_dir / "procedural.json")
        self.preference = PreferenceMemory(MemoryLayer.PREFERENCE, self.data_dir / "preference.json")
        self.project = ProjectMemory(MemoryLayer.PROJECT, self.data_dir / "project.json")

        self._stores: Dict[MemoryLayer, BaseMemoryStore] = {
            MemoryLayer.WORKING: self.working,
            MemoryLayer.EPISODIC: self.episodic,
            MemoryLayer.SEMANTIC: self.semantic,
            MemoryLayer.PROCEDURAL: self.procedural,
            MemoryLayer.PREFERENCE: self.preference,
            MemoryLayer.PROJECT: self.project,
        }

    def initialize(self) -> None:
        """Load all persistent layers from disk."""
        for layer, store in self._stores.items():
            if layer != MemoryLayer.WORKING:
                store.load()
        log.info(f"Cognitive memory initialized — {self.total_count} total entries across {len(self._stores)} layers.")

    def add(self, entry: MemoryEntry) -> None:
        """Add a memory entry to the appropriate layer."""
        store = self._stores.get(entry.layer)
        if store:
            store.add(entry)
            if entry.layer != MemoryLayer.WORKING:
                store.save()

    def add_fact(self, key: str, value: str, layer: MemoryLayer = MemoryLayer.SEMANTIC,
                 importance: float = 0.5, tags: Optional[List[str]] = None, source: str = "",
                 project: str = "") -> None:
        """Convenience: add a simple fact."""
        entry = MemoryEntry(
            key=key, value=value, layer=layer,
            importance=importance, tags=tags or [],
            source=source, project=project,
        )
        self.add(entry)

    def search(self, query: str, top_k: int = 5, layers: Optional[List[MemoryLayer]] = None) -> List[Tuple[MemoryEntry, float]]:
        """Search across specified layers (default: all persistent layers)."""
        target_layers = layers or [l for l in MemoryLayer if l != MemoryLayer.WORKING]
        all_results: List[Tuple[MemoryEntry, float]] = []
        for layer in target_layers:
            store = self._stores.get(layer)
            if store:
                results = store.search(query, top_k * 2)
                all_results.extend(results)

        # Sort by score and deduplicate by key
        all_results.sort(key=lambda x: -x[1])
        seen_keys = set()
        deduped = []
        for entry, score in all_results:
            if entry.key not in seen_keys:
                seen_keys.add(entry.key)
                deduped.append((entry, score))
                if len(deduped) >= top_k:
                    break
        return deduped

    def get(self, key: str) -> Optional[MemoryEntry]:
        """Get a memory entry by key across all layers."""
        for store in self._stores.values():
            entry = store.get(key)
            if entry:
                return entry
        return None

    def delete(self, key: str) -> bool:
        """Delete a memory entry from all layers."""
        deleted = False
        for store in self._stores.values():
            if store.delete(key):
                deleted = True
                if store.layer != MemoryLayer.WORKING:
                    store.save()
        return deleted

    def consolidate(self) -> Dict[str, Any]:
        """
        Memory consolidation pass:
          1. Remove expired memories
          2. Decay low-importance, rarely-accessed memories older than 90 days
          3. Promote episodic memories accessed 5+ times with importance >= 0.5 to semantic
        """
        stats = {"expired": 0, "decayed": 0, "promoted": 0}

        # 1. Remove expired
        for store in self._stores.values():
            if store.layer == MemoryLayer.WORKING:
                continue
            before = len(store._entries)
            store._entries = [e for e in store._entries if not e.is_expired()]
            removed = before - len(store._entries)
            stats["expired"] += removed

        # 2. Decay old, low-importance memories
        cutoff = datetime.now() - timedelta(days=90)
        for store in self._stores.values():
            if store.layer == MemoryLayer.WORKING:
                continue
            to_decay = []
            for e in store._entries:
                try:
                    created = datetime.fromisoformat(e.created)
                    if created < cutoff and e.importance < 0.3 and e.access_count < 2:
                        to_decay.append(e)
                except (ValueError, TypeError):
                    pass
            for e in to_decay:
                e.importance *= 0.8
                stats["decayed"] += 1

        # 3. Promote episodic → semantic
        to_promote = []
        for entry in self.episodic._entries:
            if entry.access_count >= 5 and entry.importance >= 0.5:
                # Check if semantic already has this key
                existing = self.semantic.get(entry.key)
                if not existing:
                    to_promote.append(entry)

        for entry in to_promote:
            promoted = MemoryEntry(
                key=entry.key, value=entry.value,
                layer=MemoryLayer.SEMANTIC,
                importance=min(entry.importance + 0.1, 1.0),
                confidence=entry.confidence,
                access_count=entry.access_count,
                tags=list(entry.tags),
                source=f"promoted_from_episodic:{entry.created}",
                project=entry.project,
            )
            self.semantic.add(promoted)
            stats["promoted"] += 1

        # Save all
        for store in self._stores.values():
            if store.layer != MemoryLayer.WORKING:
                store.save()

        log.info(f"Consolidation complete: {stats}")
        return stats

    def detect_contradictions(self) -> List[Dict[str, Any]]:
        """Find entries with the same key but different values across layers."""
        by_key: Dict[str, List[MemoryEntry]] = {}
        for store in self._stores.values():
            for entry in store.all_entries():
                by_key.setdefault(entry.key, []).append(entry)

        contradictions = []
        for key, entries in by_key.items():
            if len(entries) > 1:
                values = set(e.value for e in entries)
                if len(values) > 1:
                    contradictions.append({
                        "key": key,
                        "values": [{"value": e.value, "layer": e.layer.value, "importance": e.importance} for e in entries],
                    })
        return contradictions

    def build_context(self, query: str = "", max_chars: int = 2000) -> str:
        """Build a context string for injection into system prompts."""
        lines = []
        total_chars = 0

        # Always include user identity from semantic layer
        identity = self.search("user name", top_k=1, layers=[MemoryLayer.SEMANTIC])
        for entry, _ in identity:
            if "name" in entry.key.lower():
                lines.append(f"- User's name is {entry.value}")
                total_chars += len(lines[-1])

        if query:
            results = self.search(query, top_k=8)
        else:
            # Recent entries from semantic
            results = [(e, e.importance) for e in self.semantic.all_entries()[-5:]]

        for entry, score in results:
            line = f"- {entry.value}"
            if total_chars + len(line) > max_chars:
                break
            lines.append(line)
            total_chars += len(line)

        return "\n".join(lines) if lines else ""

    def save_all(self) -> None:
        """Persist all persistent layers."""
        for store in self._stores.values():
            if store.layer != MemoryLayer.WORKING:
                store.save()

    @property
    def total_count(self) -> int:
        return sum(len(s.all_entries()) for s in self._stores.values())

    def get_stats(self) -> Dict[str, int]:
        """Return entry counts per layer."""
        return {layer.value: len(store.all_entries()) for layer, store in self._stores.items()}