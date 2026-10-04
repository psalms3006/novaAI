"""The credential layer everything else trusts.

`nova_secure_store` is where OAuth tokens, API keys and the byok blob live. It
had no tests, which matters more now: `integrations/accounts.py` rests its
entire "a token never reaches the model" property on this module behaving.

These tests exercise the **file fallback**, not the OS keyring. The keyring
path writes to the real user credential store, and a test suite has no
business putting entries there. The fallback is also the path that actually
runs on a machine without a working keyring, so it is the one most worth
covering.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def store(tmp_path, monkeypatch):
    """The module with its keyring disabled and its file in a temp directory."""
    secure = importlib.import_module("nova_secure_store")

    monkeypatch.setattr(secure, "_try_keyring", lambda: None)
    monkeypatch.setattr(secure, "_fallback_path",
                        lambda: tmp_path / "secure_store.bin")
    return secure


def test_a_secret_survives_a_round_trip(store):
    store.set_secret("nova.test.token", "ya29.EXAMPLE-VALUE")
    assert store.get_secret("nova.test.token") == "ya29.EXAMPLE-VALUE"


def test_an_absent_secret_is_none_not_an_error(store):
    assert store.get_secret("nova.test.never-set") is None


def test_a_secret_can_be_replaced(store):
    store.set_secret("k", "first")
    store.set_secret("k", "second")
    assert store.get_secret("k") == "second"


def test_deleting_a_secret_really_removes_it(store):
    store.set_secret("k", "value")
    store.delete_secret("k")
    assert store.get_secret("k") is None


def test_deleting_something_absent_is_not_an_error(store):
    store.delete_secret("never-existed")        # must not raise


def test_secrets_are_not_readable_as_plain_text_on_disk(store, tmp_path):
    """The file is obfuscated at rest, so a casual read does not hand it over."""
    store.set_secret("nova.test.token", "ya29.EXAMPLE-VALUE")

    raw = (tmp_path / "secure_store.bin").read_bytes()
    assert b"ya29.EXAMPLE-VALUE" not in raw, (
        "the secret is sitting in the file verbatim"
    )
    assert b"nova.test.token" not in raw, "the key name is in the clear"


def test_several_secrets_coexist(store):
    store.set_secret("a", "1")
    store.set_secret("b", "2")
    store.delete_secret("a")
    assert store.get_secret("a") is None
    assert store.get_secret("b") == "2", "deleting one secret destroyed another"


def test_a_corrupt_store_does_not_raise(store, tmp_path):
    """A damaged file must degrade to 'no secrets', not crash NOVA at startup."""
    (tmp_path / "secure_store.bin").write_bytes(b"this is not a secret store")
    assert store.get_secret("anything") is None


def test_unicode_survives(store):
    store.set_secret("k", "paßwort-你好")
    assert store.get_secret("k") == "paßwort-你好"


def test_the_backend_reports_itself_honestly(store):
    """NOVA should be able to say whether this machine has hardware backing."""
    assert store.backend() in ("keyring", "file"), store.backend()
    assert store.is_hardware_backed() is False, (
        "keyring is disabled in this fixture; it should not claim otherwise"
    )
