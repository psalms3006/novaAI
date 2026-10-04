"""A server switched off in config is not a server that failed.

The filesystem MCP server ships disabled (NOVA_MCP_FILESYSTEM_ENABLED=0), and
every startup still logged "MCP servers failed to connect: filesystem" -- a
warning that sent the reader after a fault that did not exist.
"""
import logging

import pytest

pytest.importorskip("mcp")

from nova_mcp.bridge import MCPBridge
from nova_mcp.models import ServerConfig, TransportType


def _start(configs, caplog):
    bridge = MCPBridge()
    with caplog.at_level(logging.INFO, logger="nova.mcp.bridge"):
        results = bridge.start(configs, connect_timeout_s=10.0)
    bridge.shutdown()
    return results


def test_a_disabled_server_is_reported_as_disabled_not_failed(caplog):
    cfg = ServerConfig(name="filesystem", transport=TransportType.STDIO,
                       command="npx", args=[], enabled=False)
    results = _start([cfg], caplog)
    assert results == {"filesystem": False}
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings, [r.getMessage() for r in warnings]
    assert any("disabled" in r.getMessage() and "filesystem" in r.getMessage()
               for r in caplog.records)


def test_an_enabled_server_that_cannot_start_is_still_a_warning(caplog):
    cfg = ServerConfig(name="broken", transport=TransportType.STDIO,
                       command="nova-no-such-executable-xyz", args=[],
                       enabled=True, timeout_s=3.0)
    results = _start([cfg], caplog)
    assert results == {"broken": False}
    assert any(r.levelno == logging.WARNING and "broken" in r.getMessage()
               for r in caplog.records)
