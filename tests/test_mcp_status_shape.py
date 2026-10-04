"""/api/mcp reports every MCP tool and server, each in one shape.

The tool list was built with a dict comprehension inside a list, so however
many tools a server declared, the page was told there was exactly one -- the
last. Servers came back as a mix of bare strings and {name} objects.
"""
from __future__ import annotations

import types


def test_every_tool_and_server_is_listed(monkeypatch):
    from desk import bridge

    class FakeBridge:
        def servers(self):
            return ["filesystem", types.SimpleNamespace(name="github")]

        def gemini_declarations(self):
            return [{"name": "read_file"}, {"name": "write_file"}, {"name": "list_dir"}]

    monkeypatch.setattr(bridge._nova, "HAS_MCP", True, raising=False)
    monkeypatch.setattr(bridge, "_ns", lambda name: FakeBridge() if name == "_mcp_bridge" else None)

    status = bridge._mcp_status()
    assert status["enabled"] is True
    assert status["tools"] == [{"name": "read_file"}, {"name": "write_file"}, {"name": "list_dir"}]
    assert status["servers"] == [{"name": "filesystem"}, {"name": "github"}]
    assert status["connected_servers"] == 2


def test_disabled_mcp_reports_nothing(monkeypatch):
    from desk import bridge

    monkeypatch.setattr(bridge._nova, "HAS_MCP", False, raising=False)
    status = bridge._mcp_status()
    assert status["enabled"] is False
    assert status["tools"] == [] and status["servers"] == []
    assert "disabled" in status["message"]
