"""The window has to keep hearing the voice session for as long as it runs.

Observed 2026-09-24: voice worked, the HUD did not follow it -- VOICE IDLE,
an empty conversation panel, ENGINE ---- and the orb flipping to "error"
twenty seconds in, because not one live event reached the window.

Root cause: ``nova.is_online()`` called ``socket.setdefaulttimeout(4.0)``,
which is process-wide. The bridge polls it for status, so every connection
accepted afterwards -- including the window's voice socket, on which the
browser never sends anything -- timed out after four quiet seconds. The
server's reader took the timeout for a close, the next event ended the
handler, and Werkzeug wrote a plain "HTTP/1.1 200 OK" onto the upgraded
socket; the browser saw close code 1006. It also never closed the probe
socket it opened.
"""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest

# The checks on the previous window (desk/static) that lived here moved to
# tests/test_desk_ui_behaviour.py when the interface became desk/ui.


def test_the_connectivity_probe_leaves_the_process_timeout_alone(monkeypatch):
    import nova

    closed = []

    class FakeConn:
        def close(self):
            closed.append(True)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    monkeypatch.setattr(nova.socket, "create_connection", lambda *a, **k: FakeConn())
    before = socket.getdefaulttimeout()
    try:
        assert nova.is_online() is True
        assert socket.getdefaulttimeout() == before
        assert closed, "the probe socket was left open"
    finally:
        socket.setdefaulttimeout(before)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def running_bridge():
    # One server per module: run_desk_server registers its socket routes as
    # it starts, so a second start in the same process does not come up.
    from desk import bridge
    # Earlier tests drive the same Flask app through test_client(); Flask then
    # refuses the socket routes run_desk_server registers as it starts, and
    # the server thread dies. In the app the server always starts first.
    bridge.app._got_first_request = False
    port = _free_port()
    threading.Thread(target=bridge.run_desk_server, args=({},),
                     kwargs={"port": port}, daemon=True).start()
    deadline = time.time() + 20
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            break
        except OSError:
            if time.time() > deadline:
                pytest.fail("the desk server did not come up")
            time.sleep(0.1)
    yield bridge, port
    bridge.shutdown_desk_server()


def test_a_quiet_voice_socket_survives_a_process_wide_timeout(running_bridge):
    """Even if some library sets a global default, the window stays attached."""
    import websocket
    from desk import live_session as ls

    bridge, port = running_bridge
    before = socket.getdefaulttimeout()
    socket.setdefaulttimeout(1.0)
    try:
        ws = websocket.create_connection(
            f"ws://127.0.0.1:{port}/api/live/ws?token={bridge.run_token}", timeout=10)
    finally:
        socket.setdefaulttimeout(before)
    try:
        time.sleep(1.8)    # longer than the timeout, with the browser silent
        ls.get_live_manager()._publish(ls.LiveEvent("state", state="speaking"))
        ws.settimeout(5)
        got = json.loads(ws.recv())
        assert got["type"] == "state" and got["state"] == "speaking"
    finally:
        ws.close()


def test_the_hud_poll_reports_the_voice_state_the_session_published(running_bridge):
    import urllib.request

    import websocket
    from desk import live_session as ls

    bridge, port = running_bridge
    ws = websocket.create_connection(
        f"ws://127.0.0.1:{port}/api/live/ws?token={bridge.run_token}", timeout=10)
    try:
        time.sleep(0.3)
        ls.get_live_manager()._publish(ls.LiveEvent("state", state="speaking"))
        ws.settimeout(5)
        ws.recv()
        time.sleep(0.2)
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/system",
                                     headers={"X-NOVA-Desk": bridge.run_token})
        body = json.loads(urllib.request.urlopen(req, timeout=30).read())
        assert body["voice"]["state"] == "speaking"
    finally:
        ws.close()


def test_the_hud_shows_the_voice_engine_and_its_latency(running_bridge, monkeypatch):
    """A voice conversation bypasses the router, so ENGINE read ---- and
    LATENCY -- for the whole of it."""
    import urllib.request

    from desk import live_session as ls

    bridge, port = running_bridge
    mgr = ls.get_live_manager()
    trace = ls.VoiceTrace("t")
    trace.last_turn_ms = 963
    monkeypatch.setattr(mgr, "_state", ls.LiveState.STREAMING)
    monkeypatch.setattr(mgr, "_model", "models/gemini-3.1-flash-live-preview")
    monkeypatch.setattr(mgr, "_trace", trace)

    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/system",
                                 headers={"X-NOVA-Desk": bridge.run_token})
    intel = json.loads(urllib.request.urlopen(req, timeout=30).read())["intelligence"]
    assert intel["model"] == "gemini-3.1-flash-live-preview"
    assert intel["last_turn_ms"] == 963
