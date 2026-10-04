"""NOVA-to-NOVA identity and message protocol.

Every test here is an attack the protocol has to survive, or a property the
brief requires. They exist because this is the one subsystem where a mistake
is not a bug report but a breach: a remote NOVA is an external system acting
for someone else, and it must never be able to speak as, or on behalf of, the
NOVA receiving its messages.
"""
from __future__ import annotations

import base64
import copy
import time

import pytest

from nova_core.network.envelope import (
    CLOCK_SKEW_S, Envelope, EnvelopeError, ReplayGuard, derive_key,
    open_envelope, seal,
)
from nova_core.network.identity import (
    NovaIdentity, PROTOCOL_VERSION, PublicIdentity, canonical_bytes,
    fingerprint, is_valid_nova_id, normalise_nova_id, verify_signature,
)


@pytest.fixture()
def obi():
    return NovaIdentity.generate(display_name="Obi", owner_label="Samuel")


@pytest.fixture()
def ava():
    return NovaIdentity.generate(display_name="Ava", owner_label="David")


def message(sender, recipient, **kw):
    return seal(sender, recipient.public(),
                message_type=kw.pop("message_type", "text"),
                payload=kw.pop("payload", {"text": "hello"}), **kw)


# -- identity ----------------------------------------------------------------

def test_a_nova_id_is_derived_from_the_key_not_assigned(obi):
    assert obi.nova_id == fingerprint(obi.signing_public_bytes)
    assert is_valid_nova_id(obi.nova_id)


def test_two_novas_have_different_identities(obi, ava):
    assert obi.nova_id != ava.nova_id


def test_the_id_is_stable_across_reloads(obi):
    restored = NovaIdentity.from_secret_dict(obi.to_secret_dict())
    assert restored.nova_id == obi.nova_id


def test_renaming_the_assistant_does_not_change_its_identity(obi):
    """Section 58: the display name is not the identifier."""
    before = obi.nova_id
    obi.display_name = "Jarvis"
    assert obi.nova_id == before
    assert obi.public().display_name == "Jarvis"


def test_the_assistant_name_is_not_the_owner_name(obi):
    """Section 86: Obi is Samuel's NOVA, not Samuel."""
    pub = obi.public()
    assert pub.display_name == "Obi"
    assert pub.owner_label == "Samuel"
    assert pub.display_name != pub.owner_label


def test_ids_avoid_characters_people_misread():
    """Crockford's alphabet: no I, L, O or U."""
    for _ in range(40):
        ident = NovaIdentity.generate()
        body = ident.nova_id.replace("NVA-", "").replace("-", "")
        assert not (set(body) & set("ILOU")), ident.nova_id


def test_an_id_typed_without_separators_is_understood(obi):
    flat = obi.nova_id.replace("-", "")
    assert normalise_nova_id(flat) == obi.nova_id
    assert normalise_nova_id(obi.nova_id.lower()) == obi.nova_id


def test_a_malformed_id_is_rejected():
    for bad in ("", "NVA", "hello", "NVA-1234", "NVA-IIII-IIII-IIII"):
        assert not is_valid_nova_id(bad), bad


def test_a_public_identity_carries_no_private_key(obi):
    blob = str(obi.public().to_dict())
    secret = obi.to_secret_dict()["signing_private"]
    assert secret not in blob


def test_a_substituted_key_is_detected(obi, ava):
    """A hostile directory may lie about which key belongs to a name; it
    cannot make that key match the fingerprint encoded in the name."""
    forged = PublicIdentity(
        nova_id=obi.nova_id,                       # claims to be Obi
        signing_key=ava.public().signing_key,      # but is Ava's key
        exchange_key=ava.public().exchange_key,
    )
    assert not forged.verify_self_consistent()


def test_a_genuine_identity_is_self_consistent(obi):
    assert obi.public().verify_self_consistent()


def test_signing_and_verification_round_trip(obi):
    data = b"the meeting is at seven"
    assert verify_signature(obi.signing_public_bytes, data, obi.sign(data))


def test_a_signature_from_another_nova_does_not_verify(obi, ava):
    data = b"the meeting is at seven"
    assert not verify_signature(obi.signing_public_bytes, data, ava.sign(data))


def test_separate_keys_are_used_for_signing_and_key_exchange(obi):
    """Conflating them is a known footgun; the proofs do not transfer."""
    assert obi.signing_public_bytes != obi.exchange_public_bytes


def test_key_agreement_matches_in_both_directions(obi, ava):
    a = obi.shared_secret(ava.exchange_public_bytes)
    b = ava.shared_secret(obi.exchange_public_bytes)
    assert a == b


def test_a_third_nova_derives_a_different_secret(obi, ava):
    kai = NovaIdentity.generate()
    with_ava = obi.shared_secret(ava.exchange_public_bytes)
    with_kai = obi.shared_secret(kai.exchange_public_bytes)
    assert with_ava != with_kai


def test_each_direction_uses_a_different_message_key(obi, ava):
    """Reusing one key both ways invites reflection and nonce collisions."""
    shared = obi.shared_secret(ava.exchange_public_bytes)
    forward = derive_key(shared, obi.nova_id, ava.nova_id)
    backward = derive_key(shared, ava.nova_id, obi.nova_id)
    assert forward != backward


# -- the envelope ------------------------------------------------------------

def test_a_sealed_message_round_trips(obi, ava):
    env = message(obi, ava, payload={"intent": "greet", "text": "hello"})
    assert open_envelope(ava, obi.public(), env) == {"intent": "greet",
                                                     "text": "hello"}


def test_the_relay_sees_routing_but_not_contents(obi, ava):
    """Section 62: enough to route, not enough to read."""
    secret = "the budget is 250000 naira"
    env = message(obi, ava, payload={"text": secret})
    wire = str(env.to_dict())
    assert secret not in wire
    assert obi.nova_id in wire and ava.nova_id in wire
    assert env.message_type in wire


def test_a_replayed_message_is_refused(obi, ava):
    guard = ReplayGuard()
    env = message(obi, ava)
    assert open_envelope(ava, obi.public(), env, replay_guard=guard)
    with pytest.raises(EnvelopeError, match="already been delivered"):
        open_envelope(ava, obi.public(), env, replay_guard=guard)


def test_a_tampered_type_breaks_the_signature(obi, ava):
    env = message(obi, ava, message_type="text")
    env.message_type = "request_file_transfer"
    with pytest.raises(EnvelopeError, match="signature"):
        open_envelope(ava, obi.public(), env)


def test_a_tampered_payload_fails_authentication(obi, ava):
    env = message(obi, ava)
    raw = bytearray(base64.b64decode(env.ciphertext))
    raw[-1] ^= 0xFF
    env.ciphertext = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(EnvelopeError):
        open_envelope(ava, obi.public(), env)


def test_a_message_cannot_be_redirected_to_another_nova(obi, ava):
    """The signature covers the recipient, so a relay cannot re-address it."""
    kai = NovaIdentity.generate()
    env = message(obi, ava)
    with pytest.raises(EnvelopeError, match="different NOVA"):
        open_envelope(kai, obi.public(), env)


def test_rewriting_the_recipient_also_breaks_the_signature(obi, ava):
    kai = NovaIdentity.generate()
    env = message(obi, ava)
    env.recipient = kai.nova_id
    with pytest.raises(EnvelopeError):
        open_envelope(kai, obi.public(), env)


def test_an_unsigned_message_is_refused(obi, ava):
    env = message(obi, ava)
    env.signature = ""
    with pytest.raises(EnvelopeError, match="not signed"):
        open_envelope(ava, obi.public(), env)


def test_a_message_signed_by_the_wrong_nova_is_refused(obi, ava):
    """Impersonation: Kai sends, but claims to be Obi."""
    kai = NovaIdentity.generate()
    env = message(obi, ava)
    env.signature = base64.b64encode(
        kai.sign(canonical_bytes(env.signed_fields()))).decode()
    with pytest.raises(EnvelopeError, match="signature"):
        open_envelope(ava, obi.public(), env)


def test_an_expired_message_is_refused(obi, ava):
    env = message(obi, ava, expires_in=1)
    env.expires_at = time.time() - 1
    with pytest.raises(EnvelopeError, match="expired"):
        open_envelope(ava, obi.public(), env)


def test_a_message_dated_far_in_the_future_is_refused(obi, ava):
    env = message(obi, ava)
    env.timestamp = time.time() + CLOCK_SKEW_S + 60
    with pytest.raises(EnvelopeError, match="future"):
        open_envelope(ava, obi.public(), env)


def test_a_very_old_message_is_refused_even_without_an_expiry(obi, ava):
    env = message(obi, ava)
    env.timestamp = time.time() - (86400 * 2)
    with pytest.raises(EnvelopeError, match="expired"):
        open_envelope(ava, obi.public(), env)


def test_sealing_to_a_forged_identity_is_refused_before_encrypting(obi, ava):
    forged = PublicIdentity(nova_id=obi.nova_id,
                            signing_key=ava.public().signing_key,
                            exchange_key=ava.public().exchange_key)
    with pytest.raises(EnvelopeError, match="does not match"):
        seal(obi, forged, message_type="text", payload={"text": "hi"})


def test_an_incompatible_protocol_version_is_refused(obi, ava):
    env = message(obi, ava)
    env.protocol = "9.0"
    with pytest.raises(EnvelopeError, match="protocol"):
        open_envelope(ava, obi.public(), env)


def test_the_protocol_version_travels_with_every_message(obi, ava):
    """Section 88: versioning from the beginning."""
    assert message(obi, ava).protocol == PROTOCOL_VERSION
    assert "protocol" in message(obi, ava).to_dict()


def test_a_malformed_envelope_is_refused_without_raising_a_raw_error():
    with pytest.raises(EnvelopeError, match="malformed"):
        Envelope.from_dict({"sender": "NVA-0000-0000-0000"})


def test_approval_flag_is_covered_by_the_signature(obi, ava):
    """A relay must not be able to clear 'this needs the user to agree'."""
    env = message(obi, ava, requires_user_approval=True)
    env.requires_user_approval = False
    with pytest.raises(EnvelopeError, match="signature"):
        open_envelope(ava, obi.public(), env)


def test_two_messages_never_share_a_nonce(obi, ava):
    nonces = {message(obi, ava).nonce for _ in range(200)}
    assert len(nonces) == 200


def test_message_ids_are_unique(obi, ava):
    ids = {message(obi, ava).message_id for _ in range(200)}
    assert len(ids) == 200


# -- replay guard ------------------------------------------------------------

def test_the_replay_guard_is_bounded(obi, ava):
    """An unbounded set is a memory leak an attacker controls."""
    guard = ReplayGuard(capacity=64)
    for _ in range(500):
        guard.accept(message(obi, ava))
    assert len(guard) <= 64


def test_the_guard_distinguishes_senders(obi, ava):
    kai = NovaIdentity.generate()
    guard = ReplayGuard()
    env = message(obi, ava)
    assert guard.accept(env)
    twin = copy.deepcopy(env)
    twin.sender = kai.nova_id          # same id, different sender
    assert guard.accept(twin)


# -- serialisation -----------------------------------------------------------

def test_canonical_bytes_are_order_independent():
    a = canonical_bytes({"b": 2, "a": 1})
    b = canonical_bytes({"a": 1, "b": 2})
    assert a == b


def test_an_envelope_survives_json_transport(obi, ava):
    import json
    env = message(obi, ava, payload={"text": "over the wire"})
    restored = Envelope.from_dict(json.loads(json.dumps(env.to_dict())))
    assert open_envelope(ava, obi.public(), restored) == {"text": "over the wire"}


def test_a_public_identity_survives_json_transport(obi):
    import json
    restored = PublicIdentity.from_dict(json.loads(json.dumps(obi.public().to_dict())))
    assert restored.verify_self_consistent()
    assert restored.nova_id == obi.nova_id
