"""GET /api/audio/devices lists real microphones, behind the per-run token.

The Settings screen offers these names for `mic_device`; before this route the
design could only show a made-up list. Each device appears once, even though
PortAudio reports it once per Windows audio API.
"""
from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture()
def client():
    from desk import bridge
    bridge.app.config["TESTING"] = True
    with bridge.app.test_client() as c:
        yield c, bridge.run_token


def _fake_sounddevice(monkeypatch, devices, default_in=1):
    fake = types.ModuleType("sounddevice")
    fake.query_devices = lambda: devices
    fake.default = types.SimpleNamespace(device=(default_in, 3))
    monkeypatch.setitem(sys.modules, "sounddevice", fake)


def test_requires_the_token(client):
    c, _ = client
    assert c.get("/api/audio/devices").status_code == 401


def test_lists_inputs_once_each(client, monkeypatch):
    c, token = client
    _fake_sounddevice(
        monkeypatch,
        [
            {"name": "Speakers", "max_input_channels": 0, "hostapi": 0},
            {"name": "Mic Array", "max_input_channels": 2, "hostapi": 0},
            {"name": "Headset Mic", "max_input_channels": 1, "hostapi": 0},
            {"name": "Mic Array", "max_input_channels": 2, "hostapi": 1},  # same mic, other API
        ],
    )
    r = c.get("/api/audio/devices", headers={"X-NOVA-Desk": token})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    names = [d["name"] for d in body["inputs"]]
    assert names == ["Mic Array", "Headset Mic"]
    assert next(d for d in body["inputs"] if d["name"] == "Mic Array")["default"] is True


def test_reports_a_missing_audio_stack_instead_of_failing(client, monkeypatch):
    c, token = client
    monkeypatch.setitem(sys.modules, "sounddevice", None)  # import raises
    body = c.get("/api/audio/devices", headers={"X-NOVA-Desk": token}).get_json()
    assert body["ok"] is False and body["inputs"] == []
