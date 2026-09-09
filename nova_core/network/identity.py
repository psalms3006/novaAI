"""nova_core.network.identity — who a NOVA is, provably.

A NOVA's identity is a keypair, not a row in a database. That choice decides
almost everything else about the protocol:

  * The NOVA ID is derived from the public key, so an identifier cannot be
    claimed by anyone who does not hold the corresponding private key.
  * The relay routes messages but cannot forge them, because it never holds a
    signing key.
  * A compromised directory can lie about *which key* belongs to a name, but
    the recipient checks the key against the fingerprint encoded in the ID it
    was given, so substitution is detectable.

Two keypairs, deliberately:

    Ed25519   signing      -- proves who sent a message
    X25519    key exchange -- derives the key that encrypts it

Using one key for both is a classic footgun; the algorithms are related but
their security proofs are not, and libraries that conflate them have been
broken before. `cryptography` is used throughout. Nothing here invents a
primitive, per the brief.

The assistant's display name ("Obi") is not part of the identity. It is a
mutable attribute, exactly like a user's email in the account system.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519

log = logging.getLogger("nova.network.identity")

PROTOCOL_VERSION = "1.0"

#: Crockford base32 without I, L, O, U -- the characters people mistype when
#: reading an ID aloud or copying it off a screen.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ID_RE = re.compile(r"^NVA(-[0-9A-HJKMNP-TV-Z]{4}){3}$")

_STORE_KEY = "nova_network_identity"


class IdentityError(Exception):
    pass


def _b32(data: bytes, length: int) -> str:
    """Encode bytes in the reduced alphabet."""
    n = int.from_bytes(data, "big")
    out = []
    for _ in range(length):
        out.append(_ALPHABET[n & 31])
        n >>= 5
    return "".join(reversed(out))


def fingerprint(public_key_bytes: bytes) -> str:
    """The NOVA ID for a signing public key.

    60 bits of a SHA-256 digest, grouped for reading aloud. Short enough to
    put on a screen, long enough that generating a colliding keypair is not
    something an attacker does casually.
    """
    digest = hashlib.sha256(b"nova-id:v1:" + public_key_bytes).digest()
    body = _b32(digest[:8], 12)
    return f"NVA-{body[0:4]}-{body[4:8]}-{body[8:12]}"


def is_valid_nova_id(value: str) -> bool:
    return bool(_ID_RE.match((value or "").strip().upper()))


def normalise_nova_id(value: str) -> str:
    v = (value or "").strip().upper().replace(" ", "")
    if v and not v.startswith("NVA"):
        v = "NVA" + v.lstrip("-")
    # Accept an ID typed without separators.
    if re.fullmatch(r"NVA[0-9A-HJKMNP-TV-Z]{12}", v):
        v = f"NVA-{v[3:7]}-{v[7:11]}-{v[11:15]}"
    return v


@dataclass
class PublicIdentity:
    """What one NOVA publishes about itself. Contains no secrets."""

    nova_id: str
    signing_key: str          # base64 raw Ed25519 public key
    exchange_key: str         # base64 raw X25519 public key
    display_name: str = ""    # "Obi" -- mutable, never an identifier
    owner_label: str = ""     # "Samuel" -- what the *user* is called
    protocol: str = PROTOCOL_VERSION
    updated_at: float = 0.0

    def signing_bytes(self) -> bytes:
        return base64.b64decode(self.signing_key)

    def exchange_bytes(self) -> bytes:
        return base64.b64decode(self.exchange_key)

    def verify_self_consistent(self) -> bool:
        """Does the ID actually belong to this key?

        This is what makes a hostile directory detectable: it may hand out the
        wrong key for a name, but it cannot make that key match the
        fingerprint the user was given.
        """
        try:
            return fingerprint(self.signing_bytes()) == normalise_nova_id(self.nova_id)
        except Exception:
            return False

    def to_dict(self) -> dict:
        return {
            "nova_id": self.nova_id, "signing_key": self.signing_key,
            "exchange_key": self.exchange_key, "display_name": self.display_name,
            "owner_label": self.owner_label, "protocol": self.protocol,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PublicIdentity":
        return cls(
            nova_id=normalise_nova_id(str(data.get("nova_id", ""))),
            signing_key=str(data.get("signing_key", "")),
            exchange_key=str(data.get("exchange_key", "")),
            display_name=str(data.get("display_name", "")),
            owner_label=str(data.get("owner_label", "")),
            protocol=str(data.get("protocol", PROTOCOL_VERSION)),
            updated_at=float(data.get("updated_at", 0) or 0),
        )


class NovaIdentity:
    """This installation's keys. The private halves never leave the machine."""

    def __init__(self, signing_private: ed25519.Ed25519PrivateKey,
                 exchange_private: x25519.X25519PrivateKey,
                 display_name: str = "", owner_label: str = "",
                 created_at: float = 0.0):
        self._signing = signing_private
        self._exchange = exchange_private
        self.display_name = display_name
        self.owner_label = owner_label
        self.created_at = created_at or time.time()

    # -- derived values ----------------------------------------------------

    @property
    def signing_public_bytes(self) -> bytes:
        return self._signing.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    @property
    def exchange_public_bytes(self) -> bytes:
        return self._exchange.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    @property
    def nova_id(self) -> str:
        return fingerprint(self.signing_public_bytes)

    def public(self) -> PublicIdentity:
        return PublicIdentity(
            nova_id=self.nova_id,
            signing_key=base64.b64encode(self.signing_public_bytes).decode(),
            exchange_key=base64.b64encode(self.exchange_public_bytes).decode(),
            display_name=self.display_name, owner_label=self.owner_label,
            updated_at=time.time(),
        )

    # -- operations --------------------------------------------------------

    def sign(self, data: bytes) -> bytes:
        return self._signing.sign(data)

    def shared_secret(self, peer_exchange_public: bytes) -> bytes:
        """X25519 agreement with a peer's public key."""
        peer = x25519.X25519PublicKey.from_public_bytes(peer_exchange_public)
        return self._exchange.exchange(peer)

    # -- persistence -------------------------------------------------------

    def to_secret_dict(self) -> dict:
        return {
            "version": 1,
            "signing_private": base64.b64encode(
                self._signing.private_bytes(
                    serialization.Encoding.Raw,
                    serialization.PrivateFormat.Raw,
                    serialization.NoEncryption())).decode(),
            "exchange_private": base64.b64encode(
                self._exchange.private_bytes(
                    serialization.Encoding.Raw,
                    serialization.PrivateFormat.Raw,
                    serialization.NoEncryption())).decode(),
            "display_name": self.display_name,
            "owner_label": self.owner_label,
            "created_at": self.created_at,
        }

    @classmethod
    def from_secret_dict(cls, data: dict) -> "NovaIdentity":
        try:
            signing = ed25519.Ed25519PrivateKey.from_private_bytes(
                base64.b64decode(data["signing_private"]))
            exchange = x25519.X25519PrivateKey.from_private_bytes(
                base64.b64decode(data["exchange_private"]))
        except Exception as e:
            raise IdentityError(
                f"The stored NOVA identity could not be read ({type(e).__name__}). "
                "It may have been created by a newer version.")
        return cls(signing, exchange,
                   display_name=str(data.get("display_name", "")),
                   owner_label=str(data.get("owner_label", "")),
                   created_at=float(data.get("created_at", 0) or 0))

    @classmethod
    def generate(cls, display_name: str = "", owner_label: str = "") -> "NovaIdentity":
        return cls(ed25519.Ed25519PrivateKey.generate(),
                   x25519.X25519PrivateKey.generate(),
                   display_name=display_name, owner_label=owner_label)


# -- verification ------------------------------------------------------------

def verify_signature(public_signing_key: bytes, data: bytes,
                     signature: bytes) -> bool:
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(
            public_signing_key).verify(signature, data)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def canonical_bytes(payload: dict) -> bytes:
    """Deterministic bytes for signing.

    Sorted keys, no insignificant whitespace, UTF-8. Two implementations must
    agree byte-for-byte or every signature fails, so this is the one
    serialisation the protocol uses -- never `str(dict)` and never a JSON
    dump with default settings.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


# -- local storage -----------------------------------------------------------

_identity: NovaIdentity | None = None
_lock = threading.RLock()


def load_identity(create: bool = True) -> NovaIdentity | None:
    """This machine's NOVA identity, from the OS keystore."""
    global _identity
    with _lock:
        if _identity is not None:
            return _identity
        try:
            import nova_secure_store as store
            data = store.get_json(_STORE_KEY)
        except Exception:
            data = None

        if isinstance(data, dict) and data.get("signing_private"):
            _identity = NovaIdentity.from_secret_dict(data)
            return _identity

        if not create:
            return None

        _identity = NovaIdentity.generate()
        save_identity(_identity)
        log.info("[NET] generated NOVA identity %s", _identity.nova_id)
        return _identity


def save_identity(identity: NovaIdentity) -> None:
    import nova_secure_store as store
    store.set_json(_STORE_KEY, identity.to_secret_dict())


def reset_identity() -> None:
    """Test hook. Does not delete the stored identity."""
    global _identity
    with _lock:
        _identity = None


def set_display_name(name: str) -> NovaIdentity:
    """Rename the assistant. The NOVA ID is unaffected, by design."""
    ident = load_identity()
    ident.display_name = (name or "").strip()[:60]
    save_identity(ident)
    return ident


__all__ = [
    "NovaIdentity", "PublicIdentity", "IdentityError", "fingerprint",
    "is_valid_nova_id", "normalise_nova_id", "verify_signature",
    "canonical_bytes", "load_identity", "save_identity", "reset_identity",
    "set_display_name", "PROTOCOL_VERSION",
]
