"""Connecting Gmail must wait for Google's redirect, not for any connection.

google_auth_oauthlib's run_local_server serves exactly one connection. A
browser's speculative preconnect to the localhost redirect port -- opened,
nothing sent, closed -- used it up, and the flow failed four seconds after
the consent page opened with "Timed out waiting for response from
authorization server" (nova.log, 2026-09-24 05:30:04), before anyone could
have signed in.
"""
from __future__ import annotations

import socket
import threading
import time
import urllib.request

import pytest

from integrations import gmail


class FakeFlow:
    def __init__(self):
        self.redirect_uri = ""
        self.fetched = None
        self.credentials = object()

    def authorization_url(self, **kw):
        return "https://accounts.google.com/o/oauth2/auth?fake=1", "state"

    def fetch_token(self, authorization_response):
        self.fetched = authorization_response


def _port():
    s = socket.socket()
    s.bind(("localhost", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _later(fn, delay):
    threading.Thread(target=lambda: (time.sleep(delay), fn()), daemon=True).start()


def _stray(port):
    socket.create_connection(("localhost", port)).close()


def _get(port, path):
    try:
        urllib.request.urlopen(f"http://localhost:{port}{path}", timeout=5).read()
    except Exception:
        pass


def test_a_stray_connection_does_not_end_the_wait():
    port, flow = _port(), FakeFlow()
    _later(lambda: _stray(port), 0.3)
    _later(lambda: _get(port, "/favicon.ico"), 0.5)
    _later(lambda: _get(port, "/?state=s&code=abc"), 1.0)

    creds = gmail._consent_via_local_server(flow, port=port, timeout_s=10,
                                            open_browser=False)

    assert creds is flow.credentials
    assert "code=abc" in flow.fetched
    assert flow.redirect_uri == f"http://localhost:{port}/"


def test_google_refusing_access_is_explained():
    port, flow = _port(), FakeFlow()
    _later(lambda: _get(port, "/?error=access_denied&state=s"), 0.3)

    with pytest.raises(gmail.GmailUnavailable) as e:
        gmail._consent_via_local_server(flow, port=port, timeout_s=10,
                                        open_browser=False)
    assert "test user" in str(e.value)
    assert flow.fetched is None


def test_nobody_finishing_sign_in_times_out_cleanly():
    port, flow = _port(), FakeFlow()
    t0 = time.time()
    with pytest.raises(gmail.GmailUnavailable) as e:
        gmail._consent_via_local_server(flow, port=port, timeout_s=1.5,
                                        open_browser=False)
    assert time.time() - t0 < 6
    assert "not finished" in str(e.value)
