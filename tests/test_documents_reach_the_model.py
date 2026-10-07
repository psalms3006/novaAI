"""The person's documents are reachable from chat and voice, with sources.

The document library worked from the Library screen's search box, but no
model could use it: `rag_search` was never declared as a tool or dispatched.
"When is my next exam?" could not be answered from the timetable the person
had just added. This drives NOVA's own tool dispatcher, not the library.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def timetable_library(tmp_path, monkeypatch):
    from nova_core.rag import api as rag_api
    from nova_core.rag.library import Library, user_scope

    lib = Library(root=tmp_path / "rag", embedding_preference="auto")
    scope = user_scope("student")
    monkeypatch.setattr(rag_api, "_lib", lambda: lib)
    monkeypatch.setattr(rag_api, "_scope", lambda: scope)

    doc = tmp_path / "Exam Timetable.md"
    doc.write_text(
        "# Second Semester Examination Timetable\n\n"
        "## Mechanical Engineering\n"
        "MEE 402 Thermodynamics II: Thursday 18 June 2026, 9:00 AM, Hall B.\n\n"
        "## Computer Science\n"
        "CSC 401 Compiler Construction: Monday 22 June 2026, 1:00 PM, Lab 3.\n",
        encoding="utf-8")
    assert lib.add_file(doc, scope=scope).ok
    return lib


def test_rag_search_is_declared_to_the_model():
    import nova
    names = {d["name"] for d in nova.TOOL_DECLARATIONS}
    assert "rag_search" in names
    from desk import chat
    assert "rag_search" in {d["name"] for d in chat.TOOL_DECLARATIONS}


def test_asking_through_novas_dispatcher_returns_the_date_with_its_source(timetable_library):
    import nova
    out = nova._execute_tool_sync("rag_search", {"query": "when is the MEE 402 exam"}, {})
    assert "18 June 2026" in out, out
    assert "Exam Timetable" in out, "the answer does not say where it came from"


def test_an_empty_library_says_so_instead_of_guessing(tmp_path, monkeypatch):
    from nova_core.rag import api as rag_api
    from nova_core.rag.library import Library
    monkeypatch.setattr(rag_api, "_lib", lambda: Library(root=tmp_path / "e"))
    monkeypatch.setattr(rag_api, "_scope", lambda: "user:nobody")
    import nova
    out = nova._execute_tool_sync("rag_search", {"query": "exam"}, {})
    assert "no documents" in out.lower()


def test_reading_documents_marks_the_turn_untrusted():
    from nova_core import trust
    assert "rag_search" in trust.TAINTING_TOOLS
