"""The owner's release flow: keygen -> sign -> publish -> offered to clients."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))


@pytest.fixture()
def cloud(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + os.path.join(tmp, "t.db"))
    monkeypatch.setenv("NOVA_ENV", "test")
    monkeypatch.setenv("NOVA_SECRET_KEY", "u-" + uuid.uuid4().hex)
    monkeypatch.setenv("NOVA_ADMIN_SECRET_KEY", "a-" + uuid.uuid4().hex)
    from nova_cloud import config as cfgmod, db
    cfgmod.reset_config()
    db.reset_engine()
    from nova_cloud.app import create_app
    app = create_app()
    with app.test_client() as c:
        yield c, monkeypatch
    db.reset_engine()
    cfgmod.reset_config()


def _pub_from_output(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("NOVA_UPDATE_PUBLIC_KEY="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(out)


def test_keygen_refuses_to_put_the_private_key_in_the_repository(capsys):
    import release
    rc = release.main(["keygen", "--out", str(REPO / "tmp-keys"), "--no-passphrase",
                       "--no-repo-write"])
    assert rc == 1
    assert not (REPO / "tmp-keys").exists()


def test_keygen_sign_publish_and_offer(cloud, tmp_path, capsys):
    import release
    client, monkeypatch = cloud
    keys = tmp_path / "keys"
    assert release.main(["keygen", "--out", str(keys), "--no-passphrase",
                         "--no-repo-write"]) == 0
    pub = _pub_from_output(capsys.readouterr().out)

    installer = tmp_path / "NOVA-Setup.exe"
    installer.write_bytes(os.urandom(4096))
    assert release.main(["sign", "--installer", str(installer), "--version", "1.2.0",
                         "--url", "https://example.com/NOVA-Setup-1.2.0.exe",
                         "--key", str(keys / "nova_release_private.pem"),
                         "--min-supported", "1.0.0"]) == 0
    manifest_path = installer.with_name(installer.name + ".manifest.json")
    sig_path = installer.with_name(installer.name + ".manifest.sig")
    data = json.loads(manifest_path.read_text())
    import hashlib
    assert data["sha256"] == hashlib.sha256(installer.read_bytes()).hexdigest()
    assert data["size"] == 4096

    monkeypatch.setenv("NOVA_UPDATE_PUBLIC_KEY", pub)
    from nova_cloud import config as cfgmod
    cfgmod.reset_config()
    from nova_cloud import manage
    assert manage.main(["publish-release", "--manifest", str(manifest_path),
                        "--signature", str(sig_path)]) == 0

    j = client.get("/v1/updates/check?version=1.0.0&device_id=d1").get_json()
    assert j["latest"] == "1.2.0" and j["update"]["manifest"] == manifest_path.read_text()


def test_publishing_a_tampered_manifest_is_refused(cloud, tmp_path, capsys):
    import release
    _, monkeypatch = cloud
    keys = tmp_path / "keys"
    release.main(["keygen", "--out", str(keys), "--no-passphrase", "--no-repo-write"])
    pub = _pub_from_output(capsys.readouterr().out)
    installer = tmp_path / "NOVA-Setup.exe"
    installer.write_bytes(b"x" * 100)
    release.main(["sign", "--installer", str(installer), "--version", "1.2.0",
                  "--url", "https://example.com/a.exe",
                  "--key", str(keys / "nova_release_private.pem")])
    m = installer.with_name(installer.name + ".manifest.json")
    m.write_text(m.read_text().replace("example.com", "evil.example"))
    monkeypatch.setenv("NOVA_UPDATE_PUBLIC_KEY", pub)
    from nova_cloud import config as cfgmod, manage
    cfgmod.reset_config()
    assert manage.main(["publish-release", "--manifest", str(m), "--signature",
                        str(installer.with_name(installer.name + ".manifest.sig"))]) == 1
