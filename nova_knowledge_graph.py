"""
knowledge_graph.py — Structured knowledge graph for NOVA.

Replaces isolated flat memories with a graph of connected entities.
Nodes represent people, projects, files, concepts, tasks, etc.
Edges represent relationships between them.

Stored as JSON. Supports:
  - Entity creation and linking
  - Path queries (A -> B -> C)
  - Neighbor traversal
  - Importance scoring
  - Temporal relationships (before, after, during)
  - Bidirectional edge traversal
"""

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

NOVA_DIR = Path(os.path.expanduser("~/.nova"))
KG_FILE = NOVA_DIR / "knowledge_graph.json"


class EntityType(str, Enum):
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


class RelationType(str, Enum):
    # Structural
    PART_OF = "part_of"           # A is part of B
    CONTAINS = "contains"         # A contains B
    DEPENDS_ON = "depends_on"     # A depends on B
    RELATED_TO = "related_to"     # General relation
    # Temporal
    BEFORE = "before"             # A happened before B
    AFTER = "after"               # A happened after B
    DURING = "during"             # A happened during B
    # Social
    WORKS_WITH = "works_with"     # Person A works with person B
    REPORTS_TO = "reports_to"     # Person A reports to person B
    MANAGES = "manages"           # Person A manages person B
    # Technical
    USES = "uses"                 # Project/person A uses tool/file B
    PRODUCES = "produces"         # Process A produces output B
    LOCATED_IN = "located_in"     # A is located in B
    BELONGS_TO = "belongs_to"     # A belongs to B
    INTERESTED_IN = "interested_in"


@dataclass
class Entity:
    """A node in the knowledge graph."""
    id: str
    type: EntityType
    name: str
    properties: dict = field(default_factory=dict)
    importance: float = 0.5
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "id": self.id, "type": self.type.value, "name": self.name,
            "properties": self.properties, "importance": self.importance,
            "created": self.created, "updated": self.updated,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Entity":
        d["type"] = EntityType(d["type"])
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Edge:
    """A relationship between two entities."""
    id: str
    source_id: str
    target_id: str
    relation: RelationType
    properties: dict = field(default_factory=dict)
    weight: float = 1.0
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "id": self.id, "source_id": self.source_id,
            "target_id": self.target_id, "relation": self.relation.value,
            "properties": self.properties, "weight": self.weight,
            "created": self.created,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Edge":
        d["relation"] = RelationType(d["relation"])
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class KnowledgeGraph:
    """
    In-memory knowledge graph with JSON persistence.

    Usage:
        kg = KnowledgeGraph()

        # Create entities
        kg.add_entity(EntityType.PERSON, "Chidi", {"role": "developer"})
        kg.add_entity(EntityType.PROJECT, "NOVA", {"status": "active"})

        # Link them
        kg.add_relation("entity_1", "entity_2", RelationType.WORKS_WITH)

        # Query
        neighbors = kg.get_neighbors("entity_1")
        path = kg.find_path("entity_a", "entity_b")
        projects = kg.find_by_type(EntityType.PROJECT)
    """

    def __init__(self, path: Path = KG_FILE):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._entities: dict[str, Entity] = {}
        self._edges: list[Edge] = []
        self._edge_index: dict[str, list[Edge]] = {}  # source_id -> edges
        self._target_index: dict[str, list[Edge]] = {}  # target_id -> edges
        self._name_index: dict[str, str] = {}  # name.lower() -> entity_id
        self._counter = 0
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                with open(self.path, "r") as f:
                    data = json.load(f)
                for eid, edata in data.get("entities", {}).items():
                    e = Entity.from_dict(edata)
                    self._entities[eid] = e
                    self._name_index[e.name.lower()] = eid
                for edata in data.get("edges", []):
                    e = Edge.from_dict(edata)
                    self._edges.append(e)
                    self._edge_index.setdefault(e.source_id, []).append(e)
                    self._target_index.setdefault(e.target_id, []).append(e)
            except (json.JSONDecodeError, IOError):
                pass

    def _save(self):
        data = {
            "entities": {eid: e.to_dict() for eid, e in self._entities.items()},
            "edges": [e.to_dict() for e in self._edges],
        }
        with open(self.path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def _next_id(self, prefix: str = "ent") -> str:
        self._counter += 1
        return f"{prefix}_{int(time.time())}_{self._counter}"

    def add_entity(self, type: EntityType, name: str,
                   properties: dict | None = None,
                   importance: float = 0.5) -> str:
        """Create a new entity. Returns its ID."""
        eid = self._next_id()
        entity = Entity(
            id=eid, type=type, name=name,
            properties=properties or {}, importance=importance,
        )
        self._entities[eid] = entity
        self._name_index[name.lower()] = eid
        self._save()
        return eid

    def get_entity(self, entity_id: str) -> Entity | None:
        return self._entities.get(entity_id)

    def find_by_name(self, name: str) -> Entity | None:
        eid = self._name_index.get(name.lower())
        return self._entities.get(eid) if eid else None

    def find_by_type(self, type: EntityType) -> list[Entity]:
        return [e for e in self._entities.values() if e.type == type]

    def search(self, query: str) -> list[Entity]:
        """Search entities by name or properties."""
        query_lower = query.lower()
        results = []
        for e in self._entities.values():
            if query_lower in e.name.lower():
                results.append(e)
                continue
            for v in e.properties.values():
                if query_lower in str(v).lower():
                    results.append(e)
                    break
        return results

    def update_entity(self, entity_id: str, properties: dict | None = None,
                      importance: float | None = None) -> bool:
        entity = self._entities.get(entity_id)
        if not entity:
            return False
        if properties:
            entity.properties.update(properties)
        if importance is not None:
            entity.importance = importance
        entity.updated = datetime.now(timezone.utc).isoformat()
        self._save()
        return True

    def delete_entity(self, entity_id: str) -> bool:
        if entity_id not in self._entities:
            return False
        entity = self._entities.pop(entity_id)
        self._name_index.pop(entity.name.lower(), None)
        # Remove connected edges
        self._edges = [
            e for e in self._edges
            if e.source_id != entity_id and e.target_id != entity_id
        ]
        self._rebuild_indexes()
        self._save()
        return True

    def add_relation(self, source_id: str, target_id: str,
                     relation: RelationType, properties: dict | None = None,
                     weight: float = 1.0) -> str | None:
        """Create a directed relation. Returns edge ID or None if entities not found."""
        if source_id not in self._entities or target_id not in self._entities:
            return None

        # Check for duplicate
        for e in self._edge_index.get(source_id, []):
            if e.target_id == target_id and e.relation == relation:
                return e.id  # Already exists

        edge = Edge(
            id=self._next_id("rel"),
            source_id=source_id, target_id=target_id,
            relation=relation, properties=properties or {},
            weight=weight,
        )
        self._edges.append(edge)
        self._edge_index.setdefault(source_id, []).append(edge)
        self._target_index.setdefault(target_id, []).append(edge)
        self._save()
        return edge.id

    def get_neighbors(self, entity_id: str, relation: RelationType | None = None,
                      direction: str = "outgoing") -> list[Entity]:
        """Get entities connected to this one."""
        if direction == "outgoing":
            edges = self._edge_index.get(entity_id, [])
        elif direction == "incoming":
            edges = self._target_index.get(entity_id, [])
        else:  # both
            edges = (
                self._edge_index.get(entity_id, []) +
                self._target_index.get(entity_id, [])
            )

        if relation:
            edges = [e for e in edges if e.relation == relation]

        result = []
        seen = set()
        for e in edges:
            target_id = e.target_id if e.source_id == entity_id else e.source_id
            if target_id not in seen and target_id in self._entities:
                seen.add(target_id)
                result.append(self._entities[target_id])
        return result

    def find_path(self, start_id: str, end_id: str, max_depth: int = 5) -> list[str]:
        """BFS to find shortest path between two entities. Returns list of entity IDs."""
        if start_id not in self._entities or end_id not in self._entities:
            return []
        if start_id == end_id:
            return [start_id]

        visited = {start_id}
        queue = [(start_id, [start_id])]

        while queue and len(queue[0][1]) <= max_depth:
            current, path = queue.pop(0)
            neighbors = self.get_neighbors(current, direction="both")
            for neighbor in neighbors:
                if neighbor.id not in visited:
                    new_path = path + [neighbor.id]
                    if neighbor.id == end_id:
                        return new_path
                    visited.add(neighbor.id)
                    queue.append((neighbor.id, new_path))

        return []  # No path found

    def _rebuild_indexes(self):
        self._edge_index.clear()
        self._target_index.clear()
        for e in self._edges:
            self._edge_index.setdefault(e.source_id, []).append(e)
            self._target_index.setdefault(e.target_id, []).append(e)

    @property
    def entity_count(self) -> int:
        return len(self._entities)

    @property
    def edge_count(self) -> int:
        return len(self._edges)

    def get_stats(self) -> dict:
        type_counts = {}
        for e in self._entities.values():
            type_counts[e.type.value] = type_counts.get(e.type.value, 0) + 1
        return {
            "entities": self.entity_count,
            "edges": self.edge_count,
            "types": type_counts,
        }

    def to_context_string(self, max_entities: int = 30) -> str:
        """Build a text summary for injection into prompts."""
        # Sort by importance, take top entities
        entities = sorted(
            self._entities.values(), key=lambda e: -e.importance
        )[:max_entities]

        lines = ["Knowledge Graph:"]
        for e in entities:
            neighbors = self.get_neighbors(e.id)[:5]
            rel_str = ""
            if neighbors:
                n_names = [n.name for n in neighbors]
                rel_str = f" (connected to: {', '.join(n_names)})"
            lines.append(f"- [{e.type.value}] {e.name}{rel_str}")

        return "\n".join(lines) if len(lines) > 1 else ""