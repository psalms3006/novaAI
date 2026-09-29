"""Credentials on the desktop: keys sealed, managed model routed correctly."""
from __future__ import annotations

import json
import os
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")


@pytest.fixture()
def creds(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("NOVA_MACHINE_DIR", str(tmp_path / "NOVA"))
    monkeypatch.delenv("NOVA_ACCOUNT_ID", raising=False)
    from desk import creds as mod
    yield mod, tmp_path / "NOVA"


def test_a_stored_key_is_sealed_and_never_written_in_plaintext(creds):
    mod, d = creds
    mod.store_byok("AIza" + "x" * 35)
    assert (d / "byok.bin").exists()
    assert not (d / "api_keys.json").exists()
    assert b"AIza" not in (d / "byok.bin").read_bytes()
    assert mod.load_byok() == "AIza" + "x" * 35


def test_a_plaintext_key_from_an_older_build_is_sealed_and_removed(creds):
    mod, d = creds
    d.mkdir(parents=True, exist_ok=True)
    (d / "api_keys.json").write_text(json.dumps({"gemini_api_key": "AIza" + "y" * 35}))
    assert mod.load_byok() == "AIza" + "y" * 35
    assert not (d / "api_keys.json").exists()
    assert (d / "byok.bin").exists()


def test_the_gateway_shim_leaves_live_token_clients_and_byok_alone(creds, monkeypatch):
    mod, _ = creds
    import google.genai as genai
    monkeypatch.setattr(mod, "_shim_applied", False)
    monkeypatch.setattr(mod, "current_cloud_client", lambda: type("C", (), {
        "active": True, "get_session": lambda self: "access-token",
        "gateway_base_url": lambda self: "https://nova.example/gateway"})())
    orig = genai.Client.__init__
    try:
        mod.apply_gateway_shim(genai)
        monkeypatch.setenv("GEMINI_API_KEY", mod._SENTINEL)
        managed = genai.Client(api_key=mod._SENTINEL)
        assert managed._api_client._http_options.base_url == "https://nova.example/gateway"

        live = genai.Client(api_key="auth_tokens/abc", http_options={"api_version": "v1alpha"})
        assert "nova.example" not in str(live._api_client._http_options.base_url or "")

        monkeypatch.setenv("GEMINI_API_KEY", "AIza" + "z" * 35)
        own = genai.Client(api_key="AIza" + "z" * 35)
        assert "nova.example" not in str(own._api_client._http_options.base_url or "")
    finally:
        genai.Client.__init__ = orig


def test_choosing_your_own_key_wins_over_the_managed_model(creds, monkeypatch):
    mod, _ = creds
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    mod.store_byok("AIza" + "k" * 35)
    mod.set_many({"auth_mode": "byok"})
    monkeypatch.setattr(mod, "current_cloud_client", lambda: type("C", (), {
        "active": True, "get_session": lambda self: "t"})())
    st = mod.resolve()
    assert st["mode"] == "byok"
    assert os.environ["GEMINI_API_KEY"] == "AIza" + "k" * 35
