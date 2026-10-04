"""The account layer must behave the same on Windows, macOS and Linux.

Identity, storage locations and the fallback path are the parts most likely to
be quietly Windows-shaped, so they are asserted here rather than assumed.
"""
from __future__ import annotations

import ast
import io

import pytest

MODULES = [
    "nova_account.py", "nova_secure_store.py", "desk/account_api.py",
    "nova_cloud/app.py", "nova_cloud/models.py", "nova_cloud/security.py",
    "nova_cloud/db.py", "nova_cloud/api_auth.py", "nova_cloud/api_devices.py",
    "nova_cloud/api_sync.py", "nova_cloud/api_telemetry.py",
    "nova_cloud/api_admin.py", "nova_cloud/auth_guard.py",
    "nova_cloud/telemetry_sink.py", "nova_cloud/config.py",
]

WINDOWS_ONLY = {"winreg", "msvcrt", "_winapi", "win32api", "win32con",
                "win32gui", "pywintypes"}


@pytest.mark.parametrize("path", MODULES)
def test_no_windows_only_imports(path):
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    found = set()
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for n in names:
            if n.split(".")[0] in WINDOWS_ONLY or n == "ctypes.wintypes":
                found.add(n)
    assert not found, f"{path} imports Windows-only modules: {found}"


@pytest.mark.parametrize("platform,expected", [
    ("win32", "NOVA"),
    ("darwin", "NOVA"),
    ("linux", "nova"),
])
def test_data_directory_follows_platform_convention(monkeypatch, tmp_path,
                                                    platform, expected):
    import nova_secure_store as st
    monkeypatch.setattr(st.sys, "platform", platform)
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(st.Path, "home", staticmethod(lambda: tmp_path))
    d = st._data_dir()
    assert d.name == expected
    assert d.exists()
    if platform == "darwin":
        assert "Application Support" in str(d)


def test_file_fallback_round_trips_without_a_keyring(monkeypatch, tmp_path):
    """The path a headless Linux box actually takes."""
    import nova_secure_store as st
    monkeypatch.setattr(st, "_try_keyring", lambda: None)
    monkeypatch.setattr(st, "_data_dir", lambda: tmp_path)

    st.set_secret("token", "secret-value")
    assert st.get_secret("token") == "secret-value"
    st.set_json("session", {"user": "x"})
    assert st.get_json("session") == {"user": "x"}
    st.delete_secret("token")
    assert st.get_secret("token") is None
    assert st.backend() == "file"
    assert st.is_hardware_backed() is False


def test_the_fallback_file_is_not_plaintext(monkeypatch, tmp_path):
    import nova_secure_store as st
    monkeypatch.setattr(st, "_try_keyring", lambda: None)
    monkeypatch.setattr(st, "_data_dir", lambda: tmp_path)
    st.set_secret("refresh_token", "super-secret-refresh-token")
    raw = (tmp_path / "secure_store.bin").read_bytes()
    assert b"super-secret-refresh-token" not in raw


def test_a_tampered_fallback_file_is_refused(monkeypatch, tmp_path):
    """Corruption must read as absent, not as attacker-chosen data."""
    import nova_secure_store as st
    monkeypatch.setattr(st, "_try_keyring", lambda: None)
    monkeypatch.setattr(st, "_data_dir", lambda: tmp_path)
    st.set_secret("k", "v")
    p = tmp_path / "secure_store.bin"
    data = bytearray(p.read_bytes())
    data[-1] = data[-1] ^ 0xFF
    p.write_bytes(bytes(data))
    assert st.get_secret("k") is None


def test_device_identity_is_not_derived_from_the_machine(monkeypatch):
    """A device id must survive a rename and must not be guessable from it."""
    import socket
    import nova_account
    import nova_secure_store as st
    fake: dict = {}
    monkeypatch.setattr(st, "set_secret", lambda k, v: fake.__setitem__(k, v))
    monkeypatch.setattr(st, "get_secret", lambda k: fake.get(k))

    first = nova_account.device_identity("a@example.com")["device_id"]
    monkeypatch.setattr(socket, "gethostname", lambda: "a-completely-new-name")
    second = nova_account.device_identity("a@example.com")["device_id"]
    assert first == second, "renaming the machine changed the device identity"

    host = socket.gethostname()
    assert host not in first, "device id leaks the hostname"
    # And a different account on the same machine gets a different device.
    other = nova_account.device_identity("b@example.com")["device_id"]
    assert other != first


def test_platform_is_reported_not_assumed(monkeypatch):
    import platform as p
    import nova_account
    import nova_secure_store as st
    fake: dict = {}
    monkeypatch.setattr(st, "set_secret", lambda k, v: fake.__setitem__(k, v))
    monkeypatch.setattr(st, "get_secret", lambda k: fake.get(k))
    monkeypatch.setattr(p, "system", lambda: "Darwin")
    assert nova_account.device_descriptor("a@example.com")["platform"] == "darwin"
    monkeypatch.setattr(p, "system", lambda: "Linux")
    assert nova_account.device_descriptor("a@example.com")["platform"] == "linux"
