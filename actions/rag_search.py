"""actions.rag_search — answer from the person's own documents.

The document library (nova_core/rag: structural chunking, hybrid semantic +
keyword search, versioning, citations) existed and worked from the Library
screen's search box, but no model could reach it: `rag_search` was never
declared as a tool nor dispatched. So "when is my MEE exam?" could not be
answered from the timetable the person had just added.

Read-only. Results come back as quoted source material with a citation per
passage; the caller treats them as content, not instructions (rag_search is
in nova_core.trust's tainting set).
"""
from __future__ import annotations


def execute(args: dict) -> str:
    from nova_core.rag.api import rag_search
    return rag_search(args or {})
