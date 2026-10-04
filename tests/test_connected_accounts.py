"""Connecting an account is not the same as granting NOVA free rein over it.

Phase 1 of the external-capability work. The properties pinned here are the
ones that are expensive to retrofit once connectors exist:

* **Authentication is not authorisation.** Authorising NOVA to reach Gmail
  must not imply she may send mail; connecting Instagram must not imply she
  may delete posts. Read and write are separate grants.
* **Tokens never leave the credential layer.** They are not returned by the
  public API, not in the object's repr, not in what a connector reports, and
  not in anything the model could see.
* **Revoking means revoked**, not merely hidden from listings.

Nothing here talks to a real provider. The connectors themselves need OAuth
client credentials and, for the social platforms, provider app review -- see
the module docstring in `integrations/accounts.py`.
"""
from __future__ import annotations

import pytest

from integrations.accounts import (
    AccountStore,
    ConnectionStatus,
    Grant,
    UnknownProvider,
    PermissionDenied,
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """An account store backed by a throwaway secret vault."""
    vault: dict[str, str] = {}

    import nova_secure_store as secure

    monkeypatch.setattr(secure, "set_secret", lambda k, v: vault.__setitem__(k, v))
    monkeypatch.setattr(secure, "get_secret", lambda k: vault.get(k))
    monkeypatch.setattr(secure, "delete_secret", lambda k: vault.pop(k, None))

    s = AccountStore(path=str(tmp_path / "accounts.json"))
    s._vault = vault          # tests inspect it directly
    return s


def test_an_unconnected_provider_reports_itself_as_such(store):
    assert store.status("gmail") is ConnectionStatus.DISCONNECTED
    assert store.connected() == []


def test_connecting_grants_only_what_was_asked_for(store):
    store.connect("gmail", account="me@example.com",
                  token="ya29.SECRET-VALUE", grants=[Grant.READ])

    assert store.status("gmail") is ConnectionStatus.CONNECTED
    assert store.may("gmail", Grant.READ) is True
    assert store.may("gmail", Grant.WRITE) is False, (
        "authorising a connection silently granted permission to act with it"
    )


def test_a_write_needs_its_own_grant(store):
    store.connect("gmail", account="me@example.com", token="t", grants=[Grant.READ])
    with pytest.raises(PermissionDenied):
        store.require("gmail", Grant.WRITE)

    store.grant("gmail", Grant.WRITE)
    store.require("gmail", Grant.WRITE)          # no longer raises


def test_a_granted_scope_can_be_taken_back(store):
    store.connect("instagram", account="@me", token="t",
                  grants=[Grant.READ, Grant.WRITE])
    store.revoke_grant("instagram", Grant.WRITE)

    assert store.may("instagram", Grant.READ) is True
    assert store.may("instagram", Grant.WRITE) is False


def test_the_token_is_never_returned_by_the_public_api(store):
    store.connect("gmail", account="me@example.com",
                  token="ya29.SECRET-VALUE", grants=[Grant.READ])

    described = store.describe("gmail")
    flat = repr(described) + repr(store) + repr(store.connected())
    assert "ya29.SECRET-VALUE" not in flat, (
        "a token leaked through the object the model is shown"
    )
    assert "SECRET" not in flat


def test_the_token_is_not_written_to_the_account_file(store, tmp_path):
    store.connect("gmail", account="me@example.com",
                  token="ya29.SECRET-VALUE", grants=[Grant.READ])

    on_disk = (tmp_path / "accounts.json").read_text(encoding="utf-8")
    assert "ya29.SECRET-VALUE" not in on_disk, (
        "the token was persisted in plain metadata instead of the vault"
    )
    assert "me@example.com" in on_disk      # metadata is fine


def test_the_token_is_reachable_only_through_the_credential_layer(store):
    store.connect("gmail", account="me@example.com",
                  token="ya29.SECRET-VALUE", grants=[Grant.READ])
    assert store._vault, "nothing reached the secure store"
    assert any("ya29.SECRET-VALUE" == v for v in store._vault.values())


def test_disconnecting_removes_the_token_not_just_the_listing(store):
    store.connect("gmail", account="me@example.com",
                  token="ya29.SECRET-VALUE", grants=[Grant.READ])
    store.disconnect("gmail")

    assert store.status("gmail") is ConnectionStatus.DISCONNECTED
    assert not any("ya29.SECRET-VALUE" == v for v in store._vault.values()), (
        "the connection was hidden but the credential survived"
    )
    with pytest.raises(PermissionDenied):
        store.require("gmail", Grant.READ)


def test_connections_survive_a_restart(store, tmp_path, monkeypatch):
    store.connect("linkedin", account="me", token="t", grants=[Grant.READ])

    import nova_secure_store as secure
    monkeypatch.setattr(secure, "get_secret", lambda k: store._vault.get(k))

    reopened = AccountStore(path=str(tmp_path / "accounts.json"))
    assert reopened.status("linkedin") is ConnectionStatus.CONNECTED
    assert reopened.may("linkedin", Grant.READ) is True


def test_an_unknown_provider_is_refused(store):
    with pytest.raises(UnknownProvider):
        store.connect("not_a_real_service", account="x", token="t",
                      grants=[Grant.READ])


def test_a_provider_that_cannot_publish_cannot_be_granted_write(store):
    """Analytics is read-only by nature; the grant should not be offerable."""
    with pytest.raises(PermissionDenied):
        store.connect("gmail_analytics_placeholder", account="x", token="t",
                      grants=[Grant.WRITE])


def test_describe_says_what_nova_may_do_in_words(store):
    """The model sees this, so it has to be accurate and free of secrets."""
    store.connect("gmail", account="me@example.com", token="t",
                  grants=[Grant.READ])
    described = store.describe("gmail")

    assert described["provider"] == "gmail"
    assert described["account"] == "me@example.com"
    assert described["status"] == "connected"
    assert described["grants"] == ["read"]
    assert "token" not in described
