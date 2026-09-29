"""Automatic updates on the desktop (desk.updater).

The helper script is run for real (PowerShell) against a fake install folder,
a fake installer and a fake NOVA, so success, a failing installer and a new
version that never starts are all exercised end to end -- including the
rollback that restores the working copy.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest


def _keypair():
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    k = Ed25519PrivateKey.generate()
    pub = base64.b64encode(k.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)).decode()
    return k, pub


def _manifest(version="1.1.0", url="https://example.com/NOVA-Setup.exe", sha="ab" * 32, size=10):
    return json.dumps({"version": version, "url": url, "sha256": sha, "size": size,
                       "min_supported": "0.0.0"}, sort_keys=True, separators=(",", ":"))


def _sig(k, m):
    return base64.b64encode(k.sign(m.encode())).decode()


@pytest.fixture()
def up(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVA_UPDATE_DIR", str(tmp_path / "updates"))
    from desk import updater
    return updater


# -- verification --------------------------------------------------------------

def test_a_signed_manifest_verifies(up):
    k, pub = _keypair()
    m = _manifest()
    assert up.verify(m, _sig(k, m), pub)["version"] == "1.1.0"


def test_tampering_or_the_wrong_key_is_refused(up):
    k, pub = _keypair()
    m = _manifest()
    with pytest.raises(Exception):
        up.verify(m.replace("example.com", "evil.example"), _sig(k, m), pub)
    with pytest.raises(Exception):
        up.verify(m, _sig(k, m), _keypair()[1])
    with pytest.raises(ValueError):
        up.verify(m, _sig(k, m), "")


def test_a_plain_http_download_url_is_refused(up, monkeypatch):
    monkeypatch.delenv("NOVA_UPDATE_ALLOW_HTTP", raising=False)
    k, pub = _keypair()
    m = _manifest(url="http://example.com/x.exe")
    with pytest.raises(ValueError):
        up.verify(m, _sig(k, m), pub)


def test_without_a_key_updates_say_they_are_not_configured(up, monkeypatch):
    monkeypatch.setattr(up, "public_key", lambda: "")
    monkeypatch.setenv("NOVA_CLOUD_URL", "http://127.0.0.1:9")
    assert up.check() is None
    assert up.status()["state"] == "not_configured"


# -- download --------------------------------------------------------------------

@pytest.fixture()
def file_server(tmp_path):
    root = tmp_path / "srv"
    root.mkdir()

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(root), **kw)

        def log_message(self, *a):
            pass

    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield root, f"http://127.0.0.1:{port}"
    srv.shutdown()


def test_download_is_checked_against_the_signed_hash(up, file_server, monkeypatch):
    monkeypatch.setenv("NOVA_UPDATE_ALLOW_HTTP", "1")
    root, base = file_server
    payload = os.urandom(200_000)
    (root / "NOVA-Setup.exe").write_bytes(payload)
    good = {"version": "9.0.0", "url": base + "/NOVA-Setup.exe",
            "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
    s = up.download(good)
    assert Path(s["installer"]).read_bytes() == payload
    assert up.staged()["version"] == "9.0.0"

    bad = dict(good, version="9.0.1", sha256="00" * 32)
    with pytest.raises(RuntimeError, match="SHA-256"):
        up.download(bad)
    assert not (up.update_dir() / "9.0.1" / "NOVA-Setup-9.0.1.exe").exists()


def test_a_partial_download_resumes(up, file_server, monkeypatch):
    monkeypatch.setenv("NOVA_UPDATE_ALLOW_HTTP", "1")
    root, base = file_server
    payload = os.urandom(300_000)
    (root / "NOVA-Setup.exe").write_bytes(payload)
    folder = up.update_dir() / "9.1.0"
    folder.mkdir(parents=True)
    (folder / "NOVA-Setup-9.1.0.part").write_bytes(payload[:100_000])
    m = {"version": "9.1.0", "url": base + "/NOVA-Setup.exe",
         "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
    s = up.download(m)
    assert Path(s["installer"]).read_bytes() == payload


def test_an_older_or_same_version_is_never_staged(up, file_server, monkeypatch):
    monkeypatch.setenv("NOVA_UPDATE_ALLOW_HTTP", "1")
    root, base = file_server
    (root / "old.exe").write_bytes(b"x" * 10)
    from nova_version import APP_VERSION
    up.download({"version": APP_VERSION, "url": base + "/old.exe",
                 "sha256": hashlib.sha256(b"x" * 10).hexdigest(), "size": 10})
    assert up.staged() is None


def test_the_outcome_is_recorded_once(up):
    (up.update_dir() / "result.json").write_text(json.dumps(
        {"version": "1.1.0", "from": "1.0.0", "result": "rolled_back", "error": "x"}))
    assert up.collect_result()["result"] == "rolled_back"
    assert up.collect_result() is None
    hist = json.loads((up.update_dir() / "history.json").read_text())
    assert hist[-1]["result"] == "rolled_back"


# -- the helper script, for real ------------------------------------------------------

windows_only = pytest.mark.skipif(sys.platform != "win32" or not shutil.which("powershell"),
                                  reason="the update helper is a Windows PowerShell script")


def _rig(tmp_path, installer_exit=0, app_healthy=True):
    inst = tmp_path / "Programs" / "NOVA"
    inst.mkdir(parents=True)
    (inst / "version.txt").write_text("1.0.0")
    upd = tmp_path / "updates"
    upd.mkdir(exist_ok=True)
    # Fake NOVA: under --health-check-only it writes the marker only if healthy.
    marker = upd / "health-1.1.0.ok"
    (inst / "NOVA.cmd").write_text(
        "@echo off\r\n" + (f'echo ok> "{marker}"\r\n' if app_healthy else "") + "exit /b 0\r\n")
    installer = tmp_path / "setup.cmd"
    installer.write_text(
        f'@echo off\r\necho 1.1.0> "{inst / "version.txt"}"\r\n'
        f'echo new> "{inst / "added.txt"}"\r\nexit /b {installer_exit}\r\n')
    return inst, upd, installer


def _run_helper(up, inst, upd, installer, timeout=8):
    ps1 = upd / "apply_update.ps1"
    ps1.write_text(up.HELPER_PS1, encoding="utf-8")
    subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1),
                    "-NovaPid", "0", "-Installer", str(installer), "-InstallerArgs", "/VERYSILENT",
                    "-InstallDir", str(inst), "-AppExe", str(inst / "NOVA.cmd"),
                    "-Version", "1.1.0", "-FromVersion", "1.0.0", "-UpdateDir", str(upd),
                    "-TimeoutSec", str(timeout), "-Relaunch", "0", "-RelaunchArgs", " "],
                   check=True, timeout=180)
    return json.loads((upd / "result.json").read_text(encoding="utf-8-sig"))


@windows_only
def test_helper_installs_and_keeps_the_new_version_when_it_starts(up, tmp_path):
    inst, upd, installer = _rig(tmp_path)
    res = _run_helper(up, inst, upd, installer)
    assert res["result"] == "installed"
    assert (inst / "version.txt").read_text().strip() == "1.1.0"
    assert not (upd / "rollback" / "1.0.0").exists()


@windows_only
def test_helper_restores_the_working_version_when_the_installer_fails(up, tmp_path):
    inst, upd, installer = _rig(tmp_path, installer_exit=1)
    res = _run_helper(up, inst, upd, installer)
    assert res["result"] == "rolled_back" and "installer" in res["error"]
    assert (inst / "version.txt").read_text().strip() == "1.0.0"
    assert not (inst / "added.txt").exists()


@windows_only
def test_helper_restores_the_working_version_when_the_new_one_never_starts(up, tmp_path):
    inst, upd, installer = _rig(tmp_path, app_healthy=False)
    res = _run_helper(up, inst, upd, installer, timeout=6)
    assert res["result"] == "rolled_back" and "did not start" in res["error"]
    assert (inst / "version.txt").read_text().strip() == "1.0.0"
