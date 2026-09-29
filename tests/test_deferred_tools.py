"""Deferred MCP tools and bounded tool output.

Under the budget every MCP tool is listed. Over it, the model sees only
find_tool / use_tool; use_tool is unwrapped to the real tool at the
dispatcher, so hooks and permissions judge the real call.
"""
import json

import pytest

from nova_core import hooks
from nova_tools import deferred


def _decl(name, desc):
    return {"name": name, "description": desc,
            "parameters": {"type": "OBJECT", "properties": {"q": {"type": "STRING"}}}}


MANY = [_decl(f"mcp__notion__{v}_{n}", f"{v} a Notion {n}") for v in ("create", "search", "update", "delete")
        for n in ("page", "database", "comment", "block", "user")] * 20


@pytest.fixture(autouse=True)
def _reset():
    yield
    deferred._deferred.clear()


def test_few_mcp_tools_are_listed_directly():
    few = [_decl("mcp__time__now", "current time")]
    assert deferred.arrange(few) == few
    assert not deferred.is_deferred("mcp__time__now")


def test_many_mcp_tools_are_found_on_demand():
    shown = deferred.arrange(MANY, budget=2000)
    assert [d["name"] for d in shown] == ["find_tool", "use_tool"]
    hits = json.loads(deferred.find_text("search notion database"))
    assert 1 <= len(hits) <= 5
    assert hits[0]["name"] == "mcp__notion__search_database"
    assert "parameters" in hits[0]
    assert "No connected tool" in deferred.find_text("teleport")


def test_use_tool_unwraps_to_the_real_call():
    assert deferred.unwrap("use_tool", {"name": "mcp__x__y", "args": {"q": 1}}) == ("mcp__x__y", {"q": 1})
    assert deferred.unwrap("use_tool", {"name": "mcp__x__y", "args": "junk"}) == ("mcp__x__y", {})
    assert deferred.unwrap("web_search", {"query": "a"}) == ("web_search", {"query": "a"})


def test_dispatcher_governs_a_deferred_tool_by_its_real_name():
    import nova
    deferred.arrange(MANY, budget=2000)
    seen = []
    saved = (list(hooks._pre), list(hooks._post))
    try:
        hooks.add_pre(lambda t, a, m: seen.append((t, a)) or {"deny": "stop here"})
        out = nova._execute_tool_sync("use_tool", {"name": "mcp__notion__delete_page", "args": {"q": "x"}}, {})
    finally:
        hooks._pre[:], hooks._post[:] = saved
    assert seen == [("mcp__notion__delete_page", {"q": "x"})]
    assert out == "Refused: stop here."


def test_use_tool_cannot_reach_tools_that_are_not_deferred():
    import nova
    deferred.arrange(MANY, budget=2000)
    out = nova._execute_tool_sync("use_tool", {"name": "self_editor", "args": {"action": "apply"}}, {})
    assert "not one of the connected tools" in out


def test_find_tool_through_the_dispatcher():
    import nova
    deferred.arrange(MANY, budget=2000)
    out = nova._execute_tool_sync("find_tool", {"query": "create comment"}, {})
    assert json.loads(out)[0]["name"] == "mcp__notion__create_comment"


def test_big_output_is_saved_and_cut(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVA_DATA_DIR", str(tmp_path))
    big = "x" * (deferred.OUTPUT_LIMIT + 500)
    out = deferred.cap_output("mcp__fs__read", {}, big, {})
    assert len(out) < deferred.OUTPUT_LIMIT
    saved = list((tmp_path / "tool_outputs").iterdir())
    assert len(saved) == 1 and saved[0].read_text(encoding="utf-8") == big
    assert str(saved[0]) in out
    assert deferred.cap_output("t", {}, "small", {}) is None


def test_output_cap_is_installed_on_the_dispatcher():
    import nova  # noqa: F401  (installs it)
    assert ("cap_output", "*") in hooks.registered()["post"]
