"""Which external accounts NOVA is connected to, and what she may do with them.

Connecting an account and being allowed to act with it are different things,
and conflating them is how an assistant ends up deleting someone's posts
because they once let it read their feed. Authorisation here is therefore
split in two:

    AUTHENTICATION   the provider has issued a token and NOVA holds it
    GRANT            the user has said NOVA may read, or may write

A grant is per provider and per direction. Reading a mailbox does not imply
sending from it; reading an account's analytics does not imply publishing to
it. Write grants are never implied, never inferred from a successful
connection, and never escalated by anything the model decides.

Tokens live in `nova_secure_store` (keyring where the OS provides one, an
encrypted file otherwise) and nowhere else. This module stores only metadata:
which provider, which account, which grants, when it was last checked. Nothing
returned by the public API contains a credential, because everything it
returns can end up in a prompt, a log line or a transcript.

**Status of the connectors themselves:** none exist yet, and none can be
written blind. Gmail needs a Google Cloud project with an OAuth client and a
consent screen. LinkedIn publishing needs `w_member_social`, which is granted
by app review. Instagram's Content Publishing API needs a Business or Creator
account linked to a Facebook Page, also behind review. TikTok's Content
Posting API needs approval with audited scopes. Those are account-holder
actions, so this layer is built and tested first and the connectors plug into
it when the credentials exist.
"""
from __future__ import annotations

import enum
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

import nova_paths
import nova_secure_store as _secure

log = logging.getLogger("nova.accounts")

__all__ = [
    "AccountStore", "ConnectionStatus", "Grant", "Provider",
    "AccountError", "UnknownProvider", "PermissionDenied",
    "PROVIDERS", "get_account_store",
]

ACCOUNTS_FILENAME = "connected_accounts.json"


class AccountError(Exception):
    """Base for everything this module refuses to do."""


class UnknownProvider(AccountError):
    """A provider NOVA has no connector description for."""


class PermissionDenied(AccountError):
    """The grant required for this operation has not been given."""


class Grant(str, enum.Enum):
    """What the user has allowed, per direction.

    Deliberately coarse. Fine-grained provider scopes belong to the provider's
    own OAuth consent screen; this is the layer where NOVA asks herself "am I
    allowed to act here at all", and a question the user has to answer should
    have few enough options to answer out loud.
    """
    READ = "read"
    WRITE = "write"


class ConnectionStatus(str, enum.Enum):
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    #: The token exists but the provider rejected it. Distinct from
    #: disconnected: the user needs to reauthorise, not to start over, and
    #: NOVA should say which.
    NEEDS_REAUTH = "needs_reauth"


class Provider:
    """What a provider is capable of, independent of any user's connection."""

    def __init__(self, name: str, description: str,
                 supports: Iterable[Grant]) -> None:
        self.name = name
        self.description = description
        self.supports = frozenset(supports)

    def __repr__(self) -> str:                      # pragma: no cover - debug
        return f"<Provider {self.name}>"


#: Providers this layer knows about. A provider appears here before its
#: connector exists, so the user can be told "that is a thing I could connect,
#: once you have set up the credentials" rather than "unknown".
PROVIDERS: dict[str, Provider] = {
    "gmail": Provider(
        "gmail", "Google Mail", {Grant.READ, Grant.WRITE}),
    "linkedin": Provider(
        "linkedin", "LinkedIn", {Grant.READ, Grant.WRITE}),
    "instagram": Provider(
        "instagram", "Instagram", {Grant.READ, Grant.WRITE}),
    "tiktok": Provider(
        "tiktok", "TikTok", {Grant.READ, Grant.WRITE}),
    # Read-only by nature. Present so that asking for a write grant on it
    # fails loudly rather than being quietly stored and later believed.
    "gmail_analytics_placeholder": Provider(
        "gmail_analytics_placeholder", "Mail statistics", {Grant.READ}),
}


def _vault_key(provider: str) -> str:
    return f"nova.account.{provider}.token"


class AccountStore:
    """The set of external accounts NOVA holds credentials for."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = Path(path) if path else nova_paths.data_file(ACCOUNTS_FILENAME)
        self._lock = threading.RLock()
        self._accounts: dict[str, dict[str, Any]] = {}
        self._load()

    # ── persistence ─────────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._accounts = data
        except Exception:
            log.warning("[ACCOUNTS] could not read %s; starting empty", self.path)
            self._accounts = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._accounts, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            log.warning("[ACCOUNTS] could not write %s", self.path, exc_info=True)

    # ── connecting ──────────────────────────────────────────────────────────
    def connect(self, provider: str, account: str, token: str,
                grants: Iterable[Grant]) -> None:
        """Record a connection. The token goes to the vault, never to disk here."""
        spec = PROVIDERS.get(provider)
        if spec is None:
            raise UnknownProvider(
                f"NOVA has no connector for {provider!r}. Known: "
                f"{', '.join(sorted(PROVIDERS))}."
            )

        wanted = {Grant(g) for g in grants}
        impossible = wanted - spec.supports
        if impossible:
            raise PermissionDenied(
                f"{spec.description} does not support "
                f"{', '.join(sorted(g.value for g in impossible))}."
            )

        with self._lock:
            _secure.set_secret(_vault_key(provider), token)
            self._accounts[provider] = {
                "provider": provider,
                "account": account,
                "status": ConnectionStatus.CONNECTED.value,
                "grants": sorted(g.value for g in wanted),
                "connected_at": time.time(),
                "last_verified": time.time(),
            }
            self._save()
        log.info("[ACCOUNTS] connected %s (%s) with %s",
                 provider, account, sorted(g.value for g in wanted))

    def disconnect(self, provider: str) -> None:
        """Forget the account and destroy the credential.

        Removing the listing alone would leave a usable token in the vault --
        revoked as far as the user is concerned and not revoked at all.
        """
        with self._lock:
            self._accounts.pop(provider, None)
            self._save()
        try:
            _secure.delete_secret(_vault_key(provider))
        except Exception:
            log.warning("[ACCOUNTS] token for %s may remain in the vault",
                        provider, exc_info=True)
        log.info("[ACCOUNTS] disconnected %s", provider)

    # ── grants ──────────────────────────────────────────────────────────────
    def grant(self, provider: str, grant: Grant) -> None:
        spec = PROVIDERS.get(provider)
        if spec is None:
            raise UnknownProvider(provider)
        if Grant(grant) not in spec.supports:
            raise PermissionDenied(
                f"{spec.description} does not support {Grant(grant).value}.")
        with self._lock:
            entry = self._accounts.get(provider)
            if entry is None:
                raise PermissionDenied(f"{provider} is not connected.")
            grants = set(entry.get("grants", []))
            grants.add(Grant(grant).value)
            entry["grants"] = sorted(grants)
            self._save()

    def revoke_grant(self, provider: str, grant: Grant) -> None:
        with self._lock:
            entry = self._accounts.get(provider)
            if entry is None:
                return
            grants = set(entry.get("grants", []))
            grants.discard(Grant(grant).value)
            entry["grants"] = sorted(grants)
            self._save()

    def may(self, provider: str, grant: Grant) -> bool:
        with self._lock:
            entry = self._accounts.get(provider)
            if entry is None:
                return False
            if entry.get("status") != ConnectionStatus.CONNECTED.value:
                return False
            return Grant(grant).value in set(entry.get("grants", []))

    def require(self, provider: str, grant: Grant) -> None:
        """Raise unless the grant is held. The check a connector calls."""
        if not self.may(provider, grant):
            spec = PROVIDERS.get(provider)
            label = spec.description if spec else provider
            raise PermissionDenied(
                f"NOVA is not allowed to {Grant(grant).value} on {label}. "
                f"Ask the user to grant it."
            )

    # ── reading ─────────────────────────────────────────────────────────────
    def status(self, provider: str) -> ConnectionStatus:
        with self._lock:
            entry = self._accounts.get(provider)
            if entry is None:
                return ConnectionStatus.DISCONNECTED
            try:
                return ConnectionStatus(entry.get("status", "disconnected"))
            except ValueError:
                return ConnectionStatus.DISCONNECTED

    def mark_needs_reauth(self, provider: str) -> None:
        """The provider rejected the token. Say so rather than failing quietly."""
        with self._lock:
            entry = self._accounts.get(provider)
            if entry is None:
                return
            entry["status"] = ConnectionStatus.NEEDS_REAUTH.value
            entry["last_verified"] = time.time()
            self._save()

    def describe(self, provider: str) -> dict[str, Any]:
        """A description safe to hand to the model or show the user."""
        with self._lock:
            entry = self._accounts.get(provider)
            if entry is None:
                return {"provider": provider, "status": "disconnected",
                        "grants": []}
            return {
                "provider": entry.get("provider", provider),
                "account": entry.get("account", ""),
                "status": entry.get("status", "disconnected"),
                "grants": list(entry.get("grants", [])),
                "connected_at": entry.get("connected_at"),
                "last_verified": entry.get("last_verified"),
            }

    def connected(self) -> list[dict[str, Any]]:
        with self._lock:
            names = sorted(self._accounts)
        return [self.describe(name) for name in names]

    def token(self, provider: str, grant: Grant) -> str:
        """Fetch a credential. Connectors only -- never returned to the model.

        Requires the grant, so a read-only connection cannot quietly obtain a
        token to write with.
        """
        self.require(provider, grant)
        value = _secure.get_secret(_vault_key(provider))
        if not value:
            self.mark_needs_reauth(provider)
            raise PermissionDenied(
                f"No stored credential for {provider}; it needs reconnecting.")
        return value

    def __repr__(self) -> str:
        with self._lock:
            names = sorted(self._accounts)
        return f"<AccountStore providers={names}>"


_store: Optional[AccountStore] = None


def get_account_store() -> AccountStore:
    global _store
    if _store is None:
        _store = AccountStore()
    return _store
