"""The backend's shared capability catalog: global knowledge, never credentials.

  * only the owner adds providers (manage.py catalog-add), and an entry whose
    setup looks like a credential is refused;
  * a signed-in installation can read the catalog and report pass/fail for a
    *listed* provider -- nothing else, and rate limited;
  * the desktop's discovery sees the catalog as options with `from_catalog`.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid

import pytest

from tests.test_cloud_instance_model_updates import auth, signup

GOOD = {"id": "clipgen", "name": "ClipGen", "description": "Short product video ads from a script",
        "kind": "http_api", "cost": "free tier", "requires_account": True,
        "docs_url": "https://docs.clipgen.example",
        "setup": {"base_url": "https://api.clipgen.example", "auth_header": "Authorization",
                  "auth_scheme": "Bearer", "health_path": "/v1/health"},
        "tags": ["video", "advertisement"]}


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + os.path.join(tmp, "t.db"))
    monkeypatch.setenv("NOVA_ENV", "test")
    monkeypatch.setenv("NOVA_SECRET_KEY", "user-" + uuid.uuid4().hex)
    monkeypatch.setenv("NOVA_ADMIN_SECRET_KEY", "admin-" + uuid.uuid4().hex)
    from nova_cloud import config as cfgmod, db
    from nova_cloud.auth_guard import invalidate_auth_cache
    cfgmod.reset_config()
    db.reset_engine()
    invalidate_auth_cache()
    from nova_cloud.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
    db.reset_engine()
    cfgmod.reset_config()


def test_catalog_needs_a_signed_in_installation(client):
    assert client.get("/v1/capabilities/catalog").status_code == 401


def test_owner_adds_entry_and_installations_read_it(client, tmp_path):
    from nova_cloud import manage
    f = tmp_path / "entries.json"
    f.write_text(json.dumps([GOOD]), encoding="utf-8")
    assert manage.main(["catalog-add", "--file", str(f)]) == 0
    t, _ = signup(client, "a@example.com")
    j = client.get("/v1/capabilities/catalog", headers=auth(t)).get_json()
    [p] = j["providers"]
    assert p["id"] == "clipgen" and p["capabilities"] == ["video", "advertisement"]
    assert p["setup"]["base_url"] == "https://api.clipgen.example"


@pytest.mark.parametrize("bad, why", [
    ({"setup": {"base_url": "https://x.example", "api_key": "anything"}}, "credential"),
    ({"setup": {"base_url": "https://x.example", "note": "sk-abcdefghijklmnopqrstu"}}, "credential"),
    ({"setup": {"base_url": "https://x.example", "client_secret": "s"}}, "credential"),
    ({"setup": {"base_url": "http://x.example"}}, "https"),
    ({"id": "Bad Id!"}, "id must"),
    ({"kind": "shell_script"}, "kind"),
])
def test_entries_that_carry_credentials_or_are_malformed_are_refused(bad, why):
    from nova_cloud.api_capabilities import validate_entry
    entry = {**GOOD, **bad}
    with pytest.raises(ValueError) as e:
        validate_entry(entry)
    assert why in str(e.value)


def test_reports_only_count_for_listed_providers_and_are_rate_limited(client):
    from nova_cloud.api_capabilities import upsert
    upsert(GOOD)
    t, _ = signup(client, "a@example.com")
    assert client.post("/v1/capabilities/report", headers=auth(t),
                       json={"provider_id": "made-up", "passed": True}).status_code == 404
    assert client.post("/v1/capabilities/report", headers=auth(t),
                       json={"provider_id": "clipgen", "passed": "yes"}).status_code == 400
    codes = [client.post("/v1/capabilities/report", headers=auth(t),
                         json={"provider_id": "clipgen", "passed": True}).status_code for _ in range(6)]
    assert codes == [200] * 5 + [429]
    p = client.get("/v1/capabilities/catalog", headers=auth(t)).get_json()["providers"][0]
    assert p["validated_count"] == 5


def test_disabled_entries_are_not_served(client):
    from nova_cloud.api_capabilities import upsert
    upsert({**GOOD, "enabled": False})
    t, _ = signup(client, "a@example.com")
    assert client.get("/v1/capabilities/catalog", headers=auth(t)).get_json()["providers"] == []


def test_desktop_discovery_uses_catalog_entries(tmp_path, monkeypatch):
    """What the backend serves is what the desktop's discovery ranks."""
    from nova_skills.discovery import discover
    from nova_skills.registry import CapabilityRegistry
    monkeypatch.setenv("NOVA_DATA_DIR", str(tmp_path))
    served = [dict(GOOD, capabilities=GOOD["tags"], from_catalog=True)]
    rep = discover("make a product video advertisement", CapabilityRegistry(), catalog=served)
    assert rep.stage == "catalog" and rep.catalog[0]["id"] == "clipgen"
    assert "connecting your account" in rep.catalog[0]["evaluation"]["summary"]
