"""nova_core.network.envelope — the wire format between two NOVAs.

One structure carries everything, and it is designed around what the relay is
allowed to know:

    visible to the relay      from, to, type, timestamp, expiry, message id
    encrypted end to end      the payload
    signed by the sender      all of the above, together

The relay needs the routing fields to deliver a message and to rate-limit an
abusive sender. It does not need the contents, so it does not get them.

The signature covers the routing fields *and* the ciphertext. Signing only the
payload would let a relay redirect a message to a different recipient and keep
the signature valid -- the recipient would then see a genuinely signed message
that its sender never addressed to them.

Defences, and what each one is actually for:

  replay        a nonce plus a timestamp window; a captured message cannot be
                delivered twice or held and released later
  tampering     AEAD; any edit to the ciphertext fails decryption
  redirection   the recipient is inside the signed bytes
  impersonation the sender's NOVA ID must match its signing key
  staleness     an explicit expiry, so "can you make dinner tonight" cannot be
                answered next week
"""
from __future__ import annotations

import base64
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .identity import (
    NovaIdentity, PROTOCOL_VERSION, PublicIdentity, canonical_bytes,
    fingerprint, normalise_nova_id, verify_signature,
)

log = logging.getLogger("nova.network.envelope")

#: How far out of step two clocks may be before a message is refused.
CLOCK_SKEW_S = 300
#: A message older than this is refused even if it has no explicit expiry.
MAX_AGE_S = 86400
#: Nonces remembered for replay detection. Bounded, and pruned by age.
NONCE_MEMORY = 8192


class EnvelopeError(Exception):
    """The message could not be trusted. The reason is safe to log."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def derive_key(shared_secret: bytes, sender_id: str, recipient_id: str) -> bytes:
    """One message key per direction of one conversation.

    The pair of NOVA IDs goes into the HKDF info, so the key A uses to write
    to B differs from the key B uses to write to A. Reusing one key in both
    directions invites reflection attacks and makes nonce collisions between
    the two sides possible.
    """
    return HKDF(
        algorithm=hashes.SHA256(), length=32,
        salt=b"nova-net-v1",
        info=f"{sender_id}>{recipient_id}".encode("utf-8"),
    ).derive(shared_secret)


@dataclass
class Envelope:
    """A message on the wire."""

    message_id: str
    sender: str                 # NOVA ID
    recipient: str              # NOVA ID
    message_type: str
    timestamp: float
    nonce: str                  # base64, also the AEAD nonce
    ciphertext: str             # base64
    signature: str = ""         # base64 Ed25519
    expires_at: float | None = None
    protocol: str = PROTOCOL_VERSION
    requires_user_approval: bool = False

    # -- signing ----------------------------------------------------------

    def signed_fields(self) -> dict:
        """Everything the signature covers.

        The recipient is included deliberately: without it a relay could
        redirect a message and the signature would still verify.
        """
        return {
            "protocol": self.protocol,
            "message_id": self.message_id,
            "sender": self.sender,
            "recipient": self.recipient,
            "message_type": self.message_type,
            "timestamp": round(self.timestamp, 3),
            "expires_at": (round(self.expires_at, 3)
                           if self.expires_at is not None else None),
            "requires_user_approval": bool(self.requires_user_approval),
            "nonce": self.nonce,
            "ciphertext": self.ciphertext,
        }

    def to_dict(self) -> dict:
        return {**self.signed_fields(), "signature": self.signature}

    @classmethod
    def from_dict(cls, data: dict) -> "Envelope":
        try:
            return cls(
                message_id=str(data["message_id"]),
                sender=normalise_nova_id(str(data["sender"])),
                recipient=normalise_nova_id(str(data["recipient"])),
                message_type=str(data["message_type"]),
                timestamp=float(data["timestamp"]),
                nonce=str(data["nonce"]),
                ciphertext=str(data["ciphertext"]),
                signature=str(data.get("signature", "")),
                expires_at=(float(data["expires_at"])
                            if data.get("expires_at") is not None else None),
                protocol=str(data.get("protocol", PROTOCOL_VERSION)),
                requires_user_approval=bool(
                    data.get("requires_user_approval", False)),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise EnvelopeError(f"malformed envelope ({type(e).__name__})")

    @property
    def expired(self) -> bool:
        now = time.time()
        if self.expires_at is not None and now > self.expires_at:
            return True
        return (now - self.timestamp) > MAX_AGE_S


# -- sealing -----------------------------------------------------------------

def seal(identity: NovaIdentity, recipient: PublicIdentity, *,
         message_type: str, payload: dict,
         expires_in: float | None = None,
         requires_user_approval: bool = False) -> Envelope:
    """Encrypt a payload for one recipient and sign the whole envelope."""
    if not recipient.verify_self_consistent():
        # Refuse before encrypting: a key that does not match its ID is either
        # a corrupt directory entry or an active substitution attempt.
        raise EnvelopeError(
            f"the public key offered for {recipient.nova_id} does not match "
            "that identifier")

    sender_id = identity.nova_id
    recipient_id = normalise_nova_id(recipient.nova_id)
    key = derive_key(identity.shared_secret(recipient.exchange_bytes()),
                     sender_id, recipient_id)

    nonce = os.urandom(12)
    body = canonical_bytes({"payload": payload, "sender": sender_id})
    # The recipient is bound into the AEAD as associated data as well as into
    # the signature: belt and braces on the redirection case.
    aad = f"{sender_id}|{recipient_id}|{message_type}".encode("utf-8")
    ciphertext = ChaCha20Poly1305(key).encrypt(nonce, body, aad)

    env = Envelope(
        message_id=uuid.uuid4().hex,
        sender=sender_id,
        recipient=recipient_id,
        message_type=message_type,
        timestamp=time.time(),
        nonce=_b64(nonce),
        ciphertext=_b64(ciphertext),
        expires_at=(time.time() + expires_in) if expires_in else None,
        requires_user_approval=requires_user_approval,
    )
    env.signature = _b64(identity.sign(canonical_bytes(env.signed_fields())))
    return env


def open_envelope(identity: NovaIdentity, sender: PublicIdentity,
                  env: Envelope, *, replay_guard: "ReplayGuard | None" = None
                  ) -> dict:
    """Verify and decrypt. Raises EnvelopeError on anything untrustworthy.

    The order matters: cheap checks that reject hostile traffic come before
    any cryptography, so flooding a NOVA with junk costs the attacker more
    than it costs the recipient.
    """
    if env.protocol.split(".")[0] != PROTOCOL_VERSION.split(".")[0]:
        raise EnvelopeError(
            f"protocol {env.protocol} is not compatible with "
            f"{PROTOCOL_VERSION}")

    if normalise_nova_id(env.recipient) != identity.nova_id:
        raise EnvelopeError("this message is addressed to a different NOVA")

    if normalise_nova_id(env.sender) != normalise_nova_id(sender.nova_id):
        raise EnvelopeError("the sender does not match the identity supplied")

    if not sender.verify_self_consistent():
        raise EnvelopeError(
            f"the public key for {sender.nova_id} does not match that "
            "identifier")

    now = time.time()
    if env.timestamp > now + CLOCK_SKEW_S:
        raise EnvelopeError("the message is dated in the future")
    if env.expired:
        raise EnvelopeError("the message has expired")

    if not env.signature:
        raise EnvelopeError("the message is not signed")
    if not verify_signature(sender.signing_bytes(),
                            canonical_bytes(env.signed_fields()),
                            _unb64(env.signature)):
        raise EnvelopeError("the signature does not verify")

    if replay_guard is not None and not replay_guard.accept(env):
        raise EnvelopeError("this message has already been delivered")

    key = derive_key(identity.shared_secret(sender.exchange_bytes()),
                     normalise_nova_id(env.sender), identity.nova_id)
    aad = f"{env.sender}|{env.recipient}|{env.message_type}".encode("utf-8")
    try:
        plaintext = ChaCha20Poly1305(key).decrypt(
            _unb64(env.nonce), _unb64(env.ciphertext), aad)
    except Exception:
        raise EnvelopeError("the message could not be decrypted")

    import json
    try:
        body = json.loads(plaintext.decode("utf-8"))
    except Exception:
        raise EnvelopeError("the message contents are malformed")

    if body.get("sender") != normalise_nova_id(env.sender):
        raise EnvelopeError("the sealed sender does not match the envelope")
    return body.get("payload") or {}


# -- replay -----------------------------------------------------------------

class ReplayGuard:
    """Remembers recent message ids so a captured message cannot be redelivered.

    Bounded on purpose: an unbounded set is a memory leak an attacker
    controls. Entries older than the maximum message age can be forgotten
    safely, because such a message is refused on its timestamp anyway.
    """

    def __init__(self, capacity: int = NONCE_MEMORY, window_s: float = MAX_AGE_S):
        self._seen: dict[str, float] = {}
        self._capacity = capacity
        self._window = window_s
        self._lock = threading.Lock()

    def accept(self, env: Envelope) -> bool:
        """True the first time this message is seen, False on a replay."""
        key = f"{env.sender}:{env.message_id}"
        now = time.time()
        with self._lock:
            self._prune(now)
            if key in self._seen:
                return False
            self._seen[key] = now
            return True

    def _prune(self, now: float) -> None:
        if len(self._seen) < self._capacity:
            stale = [k for k, t in self._seen.items() if now - t > self._window]
            for k in stale:
                self._seen.pop(k, None)
            return
        # Over capacity: drop the oldest half rather than clearing entirely,
        # so a flood cannot wipe the memory of legitimate recent messages.
        ordered = sorted(self._seen.items(), key=lambda kv: kv[1])
        for k, _ in ordered[:len(ordered) // 2]:
            self._seen.pop(k, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)


__all__ = ["Envelope", "EnvelopeError", "ReplayGuard", "seal", "open_envelope",
           "derive_key", "CLOCK_SKEW_S", "MAX_AGE_S"]
