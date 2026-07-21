"""
NOVA Knowledge Graph — Graph-based knowledge organization.

Inspired by VYREN's knowledge_graph.py and Obsidian's interconnected notes philosophy.
Provides bidirectional relationship mapping, BFS pathfinding, and
context assembly for improved reasoning and retrieval.

Example graph:
    User --works_on--> Project NOVA
    Project NOVA --uses--> Python
    Python --contains--> Voice Runtime
    Voice Runtime --depends_on--> Gemini Live
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from core.utils import atomic_json_write, atomic_json_read

log = logging.getLogger("nova.knowledge_graph")


class EntityType(Enum):
    PERSON = "person"
    PROJECT = "project"
    FILE = "file"
    CONCEPT = "concept"
    TASK = "task"
    MEETING = "meeting"
    DEVICE = "device"
    LOCATION = "location"
    RESEARCH = "research"
    IDEA = "idea"
    TOOL = "tool"
    WEBSITE = "website"
    APPLICATION = "application"
    ORGANIZATION = "organization"
    EVENT = "event"


class RelationType(Enum):
    # Structural
    PART_OF = "part_of"
    CONTAINS = "contains"
    DEPENDS_ON = "depends_on"
    # Temporal
    BEFORE = "before"
    AFTER = "after"
    DURING = "during"
    # Social
    WORKS_WITH = "works_with"
    REPORTS_TO = "reports_to"
    MANAGES = "manages"
    # Technical
    USES = "uses"
    PRODUCES = "produces"
    LOCATED_IN = "located_in"
    RELATED_TO = "related_to"


@dataclass
class Entity:
    """A node in the knowledge graph."""
    name: str
    entity_type: EntityType = EntityType.CONCEPT
    properties: Dict[str, Any] = field(default_factory=dict)
    importance: float = 0.5
    created: str = field(default_factory=lambda: __import__("datetime").datetime.now().isoformat())

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "entity_type": self.entity_type.value,
            "properties": self.properties,
            "importance": self.importance,
            "created": self.created,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Entity":
        d = dict(d)
        if isinstance(d.get("entity_type"), str):
            d["entity_type"] = EntityType(d["entity_type"])
        d.setdefault("properties", {})
        d.setdefault("importance", 0.5)
        d.setdefault("created", "")
        return cls(**d)


@dataclass
class Relation:
    """A directed edge in the knowledge graph."""
    source: str
    relation: RelationType
    target: str
    properties: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "relation": self.relation.value,
            "target": self.target,
            "properties": self.properties,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Relation":
        d = dict(d)
        if isinstance(d.get("relation"), str):
            d["relation"] = RelationType(d["relation"])
        d.setdefault("properties", {})
        d.setdefault("confidence", 1.0)
        return cls(**d)


class KnowledgeGraph:
    """
    In-memory knowledge graph with JSON persistence and bidirectional indexing.

    Features:
      - O(1) entity lookup by name
      - Bidirectional edge indexing for efficient neighbor traversal
      - BFS pathfinding (max depth 5)
      - Relevance-ranked search
      - Context string generation for system prompts
      - Automatic backlink maintenance
    """

    def __init__(self, persist_path: Optional[Path] = None) -> None:
        self._entities: Dict[str, Entity] = {}
        self._relations: List[Relation] = []
        self._edge_index: Dict[str, List[Relation]] = defaultdict(list)   # source → edges
        self._target_index: Dict[str, List[Relation]] = defaultdict(list)  # target → edges
        self._persist_path = persist_path or Path("nova_knowledge_graph.json")
        self._lock = threading.Lock()

    def add_entity(self, entity: Entity) -> Entity:
        """Add or update an entity."""
        with self._lock:
            existing = self._entities.get(entity.name)
            if existing and existing.entity_type == entity.entity_type:
                # Merge properties
                existing.properties.update(entity.properties)
                existing.importance = max(existing.importance, entity.importance)
                return existing
            self._entities[entity.name] = entity
            return entity

    def get_entity(self, name: str) -> Optional[Entity]:
        with self._lock:
            return self._entities.get(name)

    def remove_entity(self, name: str) -> bool:
        with self._lock:
            if name not in self._entities:
                return False
            del self._entities[name]
            # Remove all edges involving this entity
            self._relations = [
                r for r in self._relations
                if r.source != name and r.target != name
            ]
            self._rebuild_indexes()
            return True

    def add_relation(self, relation: Relation) -> None:
        """Add a directed relation between two entities."""
        with self._lock:
            self._relations.append(relation)
            self._edge_index[relation.source].append(relation)
            self._target_index[relation.target].append(relation)
            # Auto-create entities if they don't exist
            if relation.source not in self._entities:
                self._entities[relation.source] = Entity(name=relation.source)
            if relation.target not in self._entities:
                self._entities[relation.target] = Entity(name=relation.target)

    def get_neighbors(
        self,
        name: str,
        direction: str = "both",
        relation_filter: Optional[RelationType] = None,
    ) -> List[Tuple[Entity, Relation]]:
        """
        Get neighboring entities and their connecting relations.
        direction: "outgoing", "incoming", or "both"
        """
        results: List[Tuple[Entity, Relation]] = []

        with self._lock:
            if direction in ("outgoing", "both"):
                for rel in self._edge_index.get(name, []):
                    if relation_filter and rel.relation != relation_filter:
                        continue
                    target = self._entities.get(rel.target)
                    if target:
                        results.append((target, rel))

            if direction in ("incoming", "both"):
                for rel in self._target_index.get(name, []):
                    if relation_filter and rel.relation != relation_filter:
                        continue
                    source = self._entities.get(rel.source)
                    if source:
                        results.append((source, rel))

        return results

    def find_path(self, source: str, target: str, max_depth: int = 5) -> Optional[List[str]]:
        """BFS pathfinding between two entities. Returns list of entity names or None."""
        if source == target:
            return [source]

        with self._lock:
            if source not in self._entities or target not in self._entities:
                return None

            visited = {source}
            queue: deque = deque([(source, [source])])

            while queue and len(queue[0][1]) <= max_depth:
                current, path = queue.popleft()
                for neighbor, _ in self.get_neighbors(current, direction="outgoing"):
                    if neighbor.name == target:
                        return path + [neighbor.name]
                    if neighbor.name not in visited:
                        visited.add(neighbor.name)
                        queue.append((neighbor.name, path + [neighbor.name]))

        return None

    def search(self, query: str, top_k: int = 10) -> List[Tuple[Entity, float]]:
        """Search entities by name or properties. Returns [(entity, score)]."""
        query_lower = query.lower()
        results: List[Tuple[Entity, float]] = []

        with self._lock:
            for entity in self._entities.values():
                score = entity.importance
                if query_lower in entity.name.lower():
                    score += 0.5
                # Search in properties
                for v in entity.properties.values():
                    if isinstance(v, str) and query_lower in v.lower():
                        score += 0.2
                        break
                if score > 0:
                    results.append((entity, score))

        results.sort(key=lambda x: -x[1])
        return results[:top_k]

    def to_context_string(self, max_entities: int = 30, max_chars: int = 1500) -> str:
        """Generate a context string for system prompt injection."""
        # Sort by importance and take top entities
        sorted_entities = sorted(
            self._entities.values(),
            key=lambda e: -e.importance
        )[:max_entities]

        lines = []
        total_chars = 0
        for entity in sorted_entities:
            neighbors = self.get_neighbors(entity.name, direction="both")
            if not neighbors:
                continue
            parts = [f"{entity.name} ({entity.entity_type.value})"]
            for neighbor, rel in neighbors[:5]:  # Limit neighbors per entity
                parts.append(f"  --{rel.relation.value}--> {neighbor.name}")
            line = "\n".join(parts)
            if total_chars + len(line) > max_chars:
                break
            lines.append(line)
            total_chars += len(line)

        if not lines:
            return ""
        return "Knowledge Graph:\n" + "\n".join(lines)

    def get_backlinks(self, name: str) -> List[Relation]:
        """Get all relations pointing TO this entity (Obsidian-style backlinks)."""
        with self._lock:
            return list(self._target_index.get(name, []))

    def get_outlinks(self, name: str) -> List[Relation]:
        """Get all relations pointing FROM this entity."""
        with self._lock:
            return list(self._edge_index.get(name, []))

    def get_stats(self) -> Dict[str, Any]:
        """Return graph statistics."""
        with self._lock:
            return {
                "entities": len(self._entities),
                "relations": len(self._relations),
                "entity_types": dict(
                    defaultdict(int, {
                        e.entity_type.value: sum(1 for ent in self._entities.values() if ent.entity_type == e.entity_type)
                        for e in EntityType
                    })
                ),
            }

    def save(self) -> None:
        """Persist graph to JSON."""
        with self._lock:
            data = {
                "entities": [e.to_dict() for e in self._entities.values()],
                "relations": [r.to_dict() for r in self._relations],
            }
            atomic_json_write(self._persist_path, data)
            log.debug(f"Knowledge graph saved — {len(self._entities)} entities, {len(self._relations)} relations.")

    def load(self) -> None:
        """Load graph from JSON."""
        if not self._persist_path.exists():
            return
        data = atomic_json_read(self._persist_path, default={})
        with self._lock:
            self._entities = {}
            self._relations = []
            self._edge_index.clear()
            self._target_index.clear()

            for d in data.get("entities", []):
                if isinstance(d, dict):
                    entity = Entity.from_dict(d)
                    self._entities[entity.name] = entity

            for d in data.get("relations", []):
                if isinstance(d, dict):
                    rel = Relation.from_dict(d)
                    self._relations.append(rel)
                    self._edge_index[rel.source].append(rel)
                    self._target_index[rel.target].append(rel)

            log.info(f"Knowledge graph loaded — {len(self._entities)} entities, {len(self._relations)} relations.")

    def _rebuild_indexes(self) -> None:
        """Rebuild edge indexes from scratch."""
        self._edge_index.clear()
        self._target_index.clear()
        for rel in self._relations:
            self._edge_index[rel.source].append(rel)
            self._target_index[rel.target].append(rel)