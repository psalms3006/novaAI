"""
NOVA Memory — Multi-layer cognitive memory system.

Provides FAISS-based semantic search (preserved from original NOVA)
plus a 6-layer cognitive memory model inspired by VYREN's memory_v2.
"""
from memory.faiss_store import NovaFAISSMemory
from memory.layers import (
    MemoryLayer, MemoryEntry, NovaCognitiveMemory,
    WorkingMemory, EpisodicMemory, SemanticMemory,
    ProceduralMemory, PreferenceMemory, ProjectMemory,
)
from memory.knowledge_graph import KnowledgeGraph, Entity, Relation
from memory.session import NovaMemory

__all__ = [
    "NovaFAISSMemory", "NovaCognitiveMemory", "NovaMemory",
    "MemoryLayer", "MemoryEntry",
    "WorkingMemory", "EpisodicMemory", "SemanticMemory",
    "ProceduralMemory", "PreferenceMemory", "ProjectMemory",
    "KnowledgeGraph", "Entity", "Relation",
]