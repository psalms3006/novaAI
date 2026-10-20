"""The security sandbox: a room the adversarial tests can trash.

Everything in here is synthetic and disposable. The workspace fixture is a
temp directory with obviously-fake secrets (the values are decoys that could
never authenticate to anything), traversal targets, and a trap directory
outside the sandbox that no test may read through NOVA's file tools.

The suite's global conftest (tests/conftest.py) already points NOVA_DATA_DIR
and NOVA_MACHINE_DIR at throwaway directories before any module imports;
this module adds the adversarial fixtures on top and asserts the isolation
actually holds.
"""
from __future__ import annotations

import os
import sys
import zipfile
import io
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


#: A marker that must never appear in any tool output, log, or file the
#: sandbox's tools touch. If a test ever sees it, the boundary leaked.
DECOY_SECRET_VALUE = "DECOY-NOT-A-REAL-SECRET-do-not-read-7734"


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    """The adversarial workspace: synthetic secrets, traversal targets.

    Layout:
        workspace/
            notes.txt                 ordinary file — must stay readable
            report.docx.txt           ordinary content
            .env                      DECOY — must be refused
            .env.local                DECOY — must be refused
            keys/id_rsa               DECOY — must be refused
            keys/server.pem           DECOY — must be refused
            .ssh/id_ed25519           DECOY — must be refused
            .aws/credentials          DECOY — must be refused
            project/config.json       ordinary file (not a secret by name)
            evil.zip                  zip-slip archive
            evil.tar                  tar path-traversal archive
            good.zip                  well-behaved archive
    """
    root = tmp_path_factory.mktemp("sec-sandbox")
    ws = root / "workspace"
    ws.mkdir()

    (ws / "notes.txt").write_text("Meeting at 3pm with the design team.\n",
                                  encoding="utf-8")
    (ws / "project").mkdir()
    (ws / "project" / "config.json").write_text('{"theme": "dark"}',
                                                encoding="utf-8")

    # Decoy credentials. The values are fake; the *names* are what the
    # boundary must recognise and refuse.
    (ws / ".env").write_text(f"API_KEY={DECOY_SECRET_VALUE}\n", encoding="utf-8")
    (ws / ".env.local").write_text(f"DB_PASS={DECOY_SECRET_VALUE}\n",
                                   encoding="utf-8")
    (ws / "keys").mkdir()
    (ws / "keys" / "id_rsa").write_text(
        "-----BEGIN OPENSSH PRIVATE KEY-----\nDECOY\n-----END OPENSSH "
        "PRIVATE KEY-----\n", encoding="utf-8")
    (ws / "keys" / "server.pem").write_text("-----BEGIN CERTIFICATE-----\n"
                                            "DECOY\n-----END CERTIFICATE-----\n",
                                            encoding="utf-8")
    (ws / ".ssh").mkdir()
    (ws / ".ssh" / "id_ed25519").write_text("DECOY\n", encoding="utf-8")
    (ws / ".aws").mkdir()
    (ws / ".aws" / "credentials").write_text(
        f"[default]\naws_secret_access_key = {DECOY_SECRET_VALUE}\n",
        encoding="utf-8")

    # zip-slip: an entry whose name climbs out of the destination.
    slip_name = "../escaped_by_zip.txt"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("normal.txt", "fine")
        z.writestr(slip_name, "should never land")
    (ws / "evil.zip").write_bytes(buf.getvalue())

    # tar traversal: the classic ../../ member, plus an absolute one.
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as t:
        def _add(name: str, data: bytes):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        _add("fine.txt", b"ok")
        _add("../../escaped_by_tar.txt", b"should never land")
        _add("/absolute_in_tar.txt", b"should never land either")
    (ws / "evil.tar").write_bytes(tar_buf.getvalue())

    # A well-behaved archive, to prove extraction still works when honest.
    good_buf = io.BytesIO()
    with zipfile.ZipFile(good_buf, "w") as z:
        z.writestr("docs/readme.txt", "hello")
        z.writestr("data/numbers.csv", "1,2,3\n")
    (ws / "good.zip").write_bytes(good_buf.getvalue())

    return {
        "root": root,
        "workspace": ws,
        "notes": ws / "notes.txt",
        "env": ws / ".env",
        "env_local": ws / ".env.local",
        "id_rsa": ws / "keys" / "id_rsa",
        "server_pem": ws / "keys" / "server.pem",
        "ssh_key": ws / ".ssh" / "id_ed25519",
        "aws_creds": ws / ".aws" / "credentials",
        "config_json": ws / "project" / "config.json",
        "evil_zip": ws / "evil.zip",
        "evil_tar": ws / "evil.tar",
        "good_zip": ws / "good.zip",
        "escape_marker_file": root / "escaped_by_zip.txt",
    }


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Adversarial tests never touch the network. Any attempt fails loudly."""
    import socket

    def _blocked(*a, **kw):
        raise AssertionError("a security test tried to reach the network")

    monkeypatch.setattr(socket, "create_connection", _blocked, raising=False)
    yield


@pytest.fixture()
def fresh_dispatch():
    """The tool dispatcher with a clean hook chain: hooks cleared before
    and after so a test's denial cannot leak into another test, and the
    fs_policy hook (a production fixture) reinstalled explicitly — the
    module is already in sys.modules, so a bare import would not rerun
    its install_hook()."""
    from nova_core import hooks
    import nova_core.fs_policy as fs_policy
    saved_pre, saved_post = hooks._pre[:], hooks._post[:]
    hooks.clear()
    fs_policy.install_hook()
    try:
        yield
    finally:
        hooks.clear()
        for h in saved_pre:
            hooks._pre.append(h)
        for h in saved_post:
            hooks._post.append(h)
