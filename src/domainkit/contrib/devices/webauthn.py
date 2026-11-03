"""WebAuthn's checks: a passkey's registration, and its assertions.

What a relying party must check (W3C Web Authentication, Level 3, 7.1
and 7.2) of the formats passkeys use, with no library beyond
``cryptography``: the client data's type, challenge, and origin; the
authenticator data's relying party, user presence, and user
verification; the signature, by the credential's key; and the signature
counter, which never goes back. Keys are ES256 (P-256), EdDSA (Ed25519),
or RS256 (2048 bits or more). The attestation statement is not checked:
a passkey is trusted as the user's because the user who registers it
authenticated again, not because of who made it (attestation "none").
"""

import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Final

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

ES256: Final = -7
EDDSA: Final = -8
RS256: Final = -257
ALGORITHMS: Final = (ES256, EDDSA, RS256)
"""COSE algorithms a passkey's key may use, in the order offered."""
MIN_RSA_BITS: Final = 2048
MAX_CREDENTIAL_ID: Final = 1023
"""Bytes a credential id may have (Level 3)."""
MAX_CBOR_DEPTH: Final = 8
UP: Final = 0x01
UV: Final = 0x04
BE: Final = 0x08
BS: Final = 0x10
AT: Final = 0x40
ED: Final = 0x80


class WebAuthnError(ValueError):
    """What came from the browser does not hold; the message says why."""


@dataclass(frozen=True, slots=True)
class NewCredential:
    """A passkey a registration proved: its id, key, and counter."""

    credential_id: bytes
    algorithm: int
    public_key: bytes
    """SubjectPublicKeyInfo, DER."""
    sign_count: int
    backed_up: bool
    """Synced to other devices (the BS flag): a passkey, not a device-bound key."""


@dataclass(frozen=True, slots=True)
class AuthenticatorData:
    """The authenticator data's fields this relying party reads."""

    rp_id_hash: bytes
    flags: int
    sign_count: int
    credential_id: bytes | None = None
    credential_key: dict[Any, Any] | None = None


def b64decode(value: str) -> bytes:
    """Base64url, padded or not; nothing else.

    Raises:
        WebAuthnError: It is not base64url.
    """
    if (
        not isinstance(value, str)
        or not value.isascii()
        or "+" in value
        or "/" in value
    ):
        raise WebAuthnError("not base64url")
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (binascii.Error, ValueError):
        raise WebAuthnError("not base64url") from None


def b64encode(value: bytes) -> str:
    """Base64url, without padding (as WebAuthn's JSON writes it)."""
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def verify_registration(
    *,
    client_data_json: bytes,
    attestation_object: bytes,
    challenge: str,
    rp_id: str,
    origins: Collection[str],
) -> NewCredential:
    """The credential a registration's response proves.

    Raises:
        WebAuthnError: The response does not hold.
    """
    _check_client_data(client_data_json, "webauthn.create", challenge, origins)
    attestation = cbor_decode(attestation_object)
    if not isinstance(attestation, dict) or not isinstance(
        attestation.get("authData"), bytes
    ):
        raise WebAuthnError("no authenticator data")
    data = parse_authenticator_data(attestation["authData"])
    _check_authenticator(data, rp_id)
    if data.credential_id is None or data.credential_key is None:
        raise WebAuthnError("no attested credential")
    algorithm, key = cose_key(data.credential_key)
    return NewCredential(
        credential_id=data.credential_id,
        algorithm=algorithm,
        public_key=key.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
        sign_count=data.sign_count,
        backed_up=bool(data.flags & BS),
    )


def verify_assertion(
    *,
    client_data_json: bytes,
    authenticator_data: bytes,
    signature: bytes,
    challenge: str,
    rp_id: str,
    origins: Collection[str],
    algorithm: int,
    public_key: bytes,
    sign_count: int,
) -> int:
    """The new signature count, if the assertion holds for the stored key.

    Raises:
        WebAuthnError: It does not, or its count went back (a copied key).
    """
    _check_client_data(client_data_json, "webauthn.get", challenge, origins)
    data = parse_authenticator_data(authenticator_data)
    _check_authenticator(data, rp_id)
    signed = authenticator_data + hashlib.sha256(client_data_json).digest()
    if not verify_signature(algorithm, public_key, signature, signed):
        raise WebAuthnError("the signature does not hold")
    if (data.sign_count or sign_count) and data.sign_count <= sign_count:
        raise WebAuthnError("the signature count went back: the key may be copied")
    return data.sign_count


def verify_signature(
    algorithm: int, public_key: bytes, signature: bytes, data: bytes
) -> bool:
    """Whether ``signature`` of ``data`` holds for the key (SPKI DER)."""
    try:
        key = serialization.load_der_public_key(public_key)
        if algorithm == ES256 and isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(signature, data, ec.ECDSA(hashes.SHA256()))
        elif algorithm == EDDSA and isinstance(key, ed25519.Ed25519PublicKey):
            key.verify(signature, data)
        elif algorithm == RS256 and isinstance(key, rsa.RSAPublicKey):
            key.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
        else:
            return False
    except (InvalidSignature, ValueError, UnsupportedAlgorithm):
        return False
    return True


def parse_authenticator_data(data: bytes) -> AuthenticatorData:
    """The authenticator data's fields, its credential's key undecoded.

    Raises:
        WebAuthnError: It is short, or has bytes after its fields.
    """
    if len(data) < 37:
        raise WebAuthnError("authenticator data too short")
    flags = data[32]
    sign_count = int.from_bytes(data[33:37], "big")
    offset = 37
    credential_id = key = None
    if flags & AT:
        if len(data) < offset + 18:
            raise WebAuthnError("attested credential data too short")
        length = int.from_bytes(data[offset + 16 : offset + 18], "big")
        offset += 18
        credential_id = data[offset : offset + length]
        if not 0 < length <= MAX_CREDENTIAL_ID or len(credential_id) != length:
            raise WebAuthnError("bad credential id")
        offset += length
        key, offset = _decode(data, offset, 0)
        if not isinstance(key, dict):
            raise WebAuthnError("the credential's key is not a COSE key")
    if flags & ED:
        _, offset = _decode(data, offset, 0)
    if offset != len(data):
        raise WebAuthnError("bytes after the authenticator data")
    return AuthenticatorData(data[:32], flags, sign_count, credential_id, key)


def cose_key(
    key: dict[Any, Any],
) -> tuple[
    int, ec.EllipticCurvePublicKey | ed25519.Ed25519PublicKey | rsa.RSAPublicKey
]:
    """A COSE key's algorithm, and the key.

    Raises:
        WebAuthnError: It is not one of ``ALGORITHMS``, or not a key.
    """
    kty, alg = key.get(1), key.get(3)
    try:
        if alg == ES256 and kty == 2 and key.get(-1) == 1:
            x, y = key.get(-2), key.get(-3)
            if not (isinstance(x, bytes) and isinstance(y, bytes)) or (
                len(x),
                len(y),
            ) != (32, 32):
                raise WebAuthnError("bad P-256 key")
            return ES256, ec.EllipticCurvePublicKey.from_encoded_point(
                ec.SECP256R1(), b"\x04" + x + y
            )
        if alg == EDDSA and kty == 1 and key.get(-1) == 6:
            x = key.get(-2)
            if not isinstance(x, bytes):
                raise WebAuthnError("bad Ed25519 key")
            return EDDSA, ed25519.Ed25519PublicKey.from_public_bytes(x)
        if alg == RS256 and kty == 3:
            n, e = key.get(-1), key.get(-2)
            if not (isinstance(n, bytes) and isinstance(e, bytes)):
                raise WebAuthnError("bad RSA key")
            public = rsa.RSAPublicNumbers(
                int.from_bytes(e, "big"), int.from_bytes(n, "big")
            ).public_key()
            if public.key_size < MIN_RSA_BITS:
                raise WebAuthnError("RSA key too short")
            return RS256, public
    except (ValueError, UnsupportedAlgorithm) as exc:
        if isinstance(exc, WebAuthnError):
            raise
        raise WebAuthnError("not a key") from None
    raise WebAuthnError("an algorithm not offered")


def cbor_decode(data: bytes) -> Any:  # noqa: ANN401 - CBOR holds anything.
    """One CBOR item filling ``data`` (RFC 8949), definite lengths only.

    Raises:
        WebAuthnError: It is not, or holds tags, floats, or deep nesting.
    """
    value, offset = _decode(data, 0, 0)
    if offset != len(data):
        raise WebAuthnError("bytes after the CBOR item")
    return value


def _decode(data: bytes, offset: int, depth: int) -> tuple[Any, int]:
    """The CBOR item at ``offset``, and the offset after it."""
    if depth > MAX_CBOR_DEPTH:
        raise WebAuthnError("CBOR nested too deep")
    if offset >= len(data):
        raise WebAuthnError("CBOR cut short")
    initial = data[offset]
    major, info = initial >> 5, initial & 0x1F
    offset += 1
    if info < 24:
        argument = info
    elif info <= 27:
        size = 1 << (info - 24)
        if offset + size > len(data):
            raise WebAuthnError("CBOR cut short")
        argument = int.from_bytes(data[offset : offset + size], "big")
        offset += size
    else:
        raise WebAuthnError("CBOR of indefinite length")
    if major == 0:
        return argument, offset
    if major == 1:
        return -1 - argument, offset
    if major in (2, 3):
        end = offset + argument
        if end > len(data):
            raise WebAuthnError("CBOR cut short")
        raw = data[offset:end]
        if major == 2:
            return raw, end
        try:
            return raw.decode(), end
        except UnicodeDecodeError:
            raise WebAuthnError("CBOR text not UTF-8") from None
    # Each item takes a byte at least: a count past the data is cut short.
    if major == 4:
        items = []
        for _ in range(argument):
            item, offset = _decode(data, offset, depth + 1)
            items.append(item)
        return items, offset
    if major == 5:
        mapping: dict[Any, Any] = {}
        for _ in range(argument):
            key, offset = _decode(data, offset, depth + 1)
            if not isinstance(key, (int, str)) or key in mapping:
                raise WebAuthnError("bad CBOR map key")
            mapping[key], offset = _decode(data, offset, depth + 1)
        return mapping, offset
    if major == 7 and info in (20, 21, 22):
        return (False, True, None)[info - 20], offset
    raise WebAuthnError("CBOR tags and floats are not read")


def client_challenge(client_data_json: bytes) -> str:
    """The challenge the client data answers (unchecked: to find its record).

    Raises:
        WebAuthnError: It is not client data.
    """
    challenge = _client_data(client_data_json).get("challenge")
    if not isinstance(challenge, str) or not 0 < len(challenge) <= 128:
        raise WebAuthnError("no challenge")
    return challenge


def _client_data(client_data_json: bytes) -> dict[str, Any]:
    """The client data, parsed."""
    if len(client_data_json) > 4096:
        raise WebAuthnError("client data too long")
    try:
        data = json.loads(client_data_json)
    except (UnicodeDecodeError, ValueError):
        raise WebAuthnError("client data is not JSON") from None
    if not isinstance(data, dict):
        raise WebAuthnError("client data is not an object")
    return data


def _check_client_data(
    client_data_json: bytes, kind: str, challenge: str, origins: Collection[str]
) -> None:
    """The client data is of ``kind``, for ``challenge``, from one of ``origins``."""
    data = _client_data(client_data_json)
    if data.get("type") != kind:
        raise WebAuthnError(f"not {kind}")
    sent = data.get("challenge")
    if not isinstance(sent, str) or not hmac.compare_digest(
        sent.encode(), challenge.encode()
    ):
        raise WebAuthnError("another challenge")
    if data.get("origin") not in origins:
        raise WebAuthnError("another origin")
    if data.get("crossOrigin") is True:
        raise WebAuthnError("made in a frame of another origin")


def _check_authenticator(data: AuthenticatorData, rp_id: str) -> None:
    """For this relying party, the user present and verified."""
    if not hmac.compare_digest(
        data.rp_id_hash, hashlib.sha256(rp_id.encode()).digest()
    ):
        raise WebAuthnError("another relying party")
    if not data.flags & UP:
        raise WebAuthnError("the user was not present")
    if not data.flags & UV:
        raise WebAuthnError("the user was not verified")
    if data.flags & BS and not data.flags & BE:
        raise WebAuthnError("backed up, but not eligible to be")
