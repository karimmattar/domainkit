r"""Requests signed by a bound device's key.

A device binds a key it holds (a phone's Secure Enclave or Android
Keystore) with ``POST /devices``; requests it sends then carry three
headers::

    X-Device-Id: 7b1c...            # the device's id
    X-Device-Time: 1767225600123    # milliseconds since the epoch
    X-Device-Signature: MEUCIQ...   # base64url

The signature is of this message, in UTF-8::

    domainkit-device-v1
    POST
    /payments?draft=1
    1767225600123
    <the body's SHA-256, base64url>

(the lines joined by ``\\n``): the method, the path with its query as
sent, the time, and the body. ``DeviceSigned`` lets a request through
only when a device of the request's user signed it, within
``DEVICES.SIGNATURE_WINDOW`` of the server's time, and it was not sent
before: a signed request works once, whichever process reads it, and
requests sent at once pass in whatever order they arrive (ADR 0135).
Keys are P-256 (ES256, the signature DER as both platforms make it) or
Ed25519, sent as SubjectPublicKeyInfo or, for P-256, the raw point the
Secure Enclave exports.

The body is read once its device is found, the user's: a request that
names no device of theirs is refused first. A request may also say the
body's SHA-256 in ``Content-Digest`` (RFC 9530): its signature is then
checked before the body is read, and the body against it after.
``DEVICES.CONTENT_DIGEST = "required"`` refuses a request that does not
(ADR 0175).
"""

import base64
import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from starlette.requests import HTTPConnection, Request

from domainkit.apps import registry
from domainkit.cache import CacheError, caches
from domainkit.contrib.auth.integration import note_login_device, utc_now
from domainkit.contrib.auth.models import UserId
from domainkit.http.access import BasePermission

from .events import DeviceBound
from .exceptions import DeviceSignatureRefused, TooManyKeys, UnsupportedKey
from .settings import DevicesSettings
from .stores import BoundDevice, DevicesUnitOfWork
from .webauthn import EDDSA, ES256, WebAuthnError, b64decode, verify_signature

__all__ = [
    "DeviceProof",
    "DeviceSigned",
    "bind_key",
    "device_key",
    "device_proof",
    "mark_signed_by",
    "proves_key",
    "signed_message",
    "signing_device",
]

DEVICE_ID: Final = "x-device-id"
DEVICE_TIME: Final = "x-device-time"
DEVICE_SIGNATURE: Final = "x-device-signature"
DIGEST_HEADER: Final = "content-digest"
"""The body's digest the client declares (RFC 9530): ``sha-256=:<base64>:``."""
VERSION: Final = b"domainkit-device-v1"
SIGNING_DEVICE: Final = "domainkit.signing_device"
"""Where a request keeps the device that signed it, checked once."""
SIGNING_PROOF: Final = "domainkit.signing_proof"
"""Where a request keeps what its device signed, once checked."""
MAX_SIGNATURE: Final = 200
"""Base64url characters a signature may have (ES256's DER is at most 72 bytes)."""
TOUCH_EVERY: Final = timedelta(minutes=1)
"""How often a device's last use is written: not with every request."""
logger: Final = logging.getLogger(__name__)


def signed_message(method: str, target: str, time: int, body: bytes) -> bytes:
    """What a device signs for a request."""
    return _message(method, target, time, hashlib.sha256(body).digest())


def _message(method: str, target: str, time: int, digest: bytes) -> bytes:
    """What a device signs for a request whose body's SHA-256 is ``digest``."""
    encoded = base64.urlsafe_b64encode(digest).rstrip(b"=")
    return b"\n".join(
        (VERSION, method.encode(), target.encode(), str(time).encode(), encoded)
    )


def device_key(encoded: str) -> tuple[int, bytes]:
    """A device key's algorithm, and the key as SubjectPublicKeyInfo.

    Raises:
        UnsupportedKey: It is not a P-256 or Ed25519 public key.
    """
    try:
        raw = b64decode(encoded)
        if len(raw) == 65 and raw[0] == 4:
            key: Any = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
        else:
            key = serialization.load_der_public_key(raw)
    except (WebAuthnError, ValueError, UnsupportedAlgorithm):
        raise UnsupportedKey("Send a P-256 or Ed25519 public key, base64url.") from None
    if isinstance(key, ec.EllipticCurvePublicKey) and isinstance(
        key.curve, ec.SECP256R1
    ):
        algorithm = ES256
    elif isinstance(key, ed25519.Ed25519PublicKey):
        algorithm = EDDSA
    else:
        raise UnsupportedKey("Send a P-256 or Ed25519 public key, base64url.")
    return algorithm, key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


@dataclass(frozen=True, slots=True, repr=False)
class DeviceProof:
    """What a bound device signed for a request: enough to check it again later.

    The device (its id, its key's algorithm, and its public key), the
    message it signed (``signed_message``), its signature, and the time it
    signed at. With the key and the message, the signature can be checked
    again once the device is unbound.
    """

    device: BoundDevice
    signed: bytes
    signature: bytes
    signed_at: datetime

    def __repr__(self) -> str:
        """The device and the time: what it signed may name personal data."""
        return f"DeviceProof(device={self.device.id!r}, signed_at={self.signed_at!r})"


class DeviceSigned(BasePermission):
    """Requests a bound device of the user signed, now, once.

    A refusal answers ``401 device_signature_refused``.
    """

    message = "Sign this request with this device's key."

    async def has_permission(
        self,
        request: HTTPConnection,
        user: Any,  # noqa: ANN401 - any principal; only users have devices.
    ) -> bool:
        """A device of ``user`` signed the request (see the module)."""
        return await signing_device(request, user) is not None

    async def has_object_permission(
        self,
        request: HTTPConnection,
        user: Any,  # noqa: ANN401 - as ``has_permission``.
        obj: object,
    ) -> bool:
        """As ``has_permission``."""
        return await self.has_permission(request, user)

    async def refusal(
        self,
        request: HTTPConnection,
        user: Any,  # noqa: ANN401 - as ``has_permission``.
    ) -> Exception:
        """``401 device_signature_refused``."""
        return DeviceSignatureRefused()


async def signing_device(connection: HTTPConnection, user: Any) -> BoundDevice | None:  # noqa: ANN401
    """The device of ``user`` that signed the request, checked once a request."""
    if SIGNING_DEVICE not in connection.scope:
        proof = await _check(connection, user)
        connection.scope[SIGNING_PROOF] = proof
        connection.scope[SIGNING_DEVICE] = None if proof is None else proof.device
        if proof is not None:
            note_login_device(connection, proof.device.id)  # Its login is the device's.
    device: BoundDevice | None = connection.scope[SIGNING_DEVICE]
    return device


async def device_proof(connection: HTTPConnection, user: Any) -> DeviceProof | None:  # noqa: ANN401
    """What the device of ``user`` that signed the request signed, and its key.

    Checked as ``signing_device`` checks it, once a request. None when no
    device of ``user`` signed it, or the request proved a key it binds
    (``mark_signed_by``): that key had no id when it signed.
    """
    await signing_device(connection, user)
    proof: DeviceProof | None = connection.scope.get(SIGNING_PROOF)
    return proof


def mark_signed_by(request: Request, device: BoundDevice) -> None:
    """Count ``request`` as signed by ``device``, which it just bound.

    The request proved the key before it had an id (``proves_key``): the
    login it goes on to issue passes the login gates as the device's own.
    """
    request.scope[SIGNING_DEVICE] = device


async def read_signature(request: Request) -> tuple[int, bytes, bytes] | None:
    """The time, the signature, and the message signed, if they are well formed.

    None when a header is missing or malformed, or the time is outside
    ``DEVICES.SIGNATURE_WINDOW``. The signature is not checked: the caller
    checks it with the key it expects.
    """
    sent = _sent(request)
    if sent is None:
        return None
    time, signature, _ = sent
    body = await request.body()
    return time, signature, signed_message(request.method, _target(request), time, body)


def _sent(request: Request) -> tuple[int, bytes, bytes | None] | None:
    """The time, the signature, and the body's declared digest, from the headers.

    None when a header is missing or malformed, or the time is outside
    ``DEVICES.SIGNATURE_WINDOW``. The digest is None when the request
    declares none (no ``Content-Digest``, or none of SHA-256), or a
    malformed one. Nothing of the body is read.
    """
    headers = request.headers
    try:
        time = int(headers.get(DEVICE_TIME, ""))
        encoded = headers.get(DEVICE_SIGNATURE, "")
        if len(encoded) > MAX_SIGNATURE:
            return None
        signature = b64decode(encoded)
    except (ValueError, WebAuthnError):
        return None
    window = registry.settings(DevicesSettings).SIGNATURE_WINDOW
    now = int(utc_now().timestamp() * 1000)
    if abs(now - time) > window.total_seconds() * 1000:
        return None
    return time, signature, _declared(headers.get(DIGEST_HEADER, ""))


def _declared(field: str) -> bytes | None:
    """The SHA-256 a ``Content-Digest`` field declares, or None (RFC 9530).

    The field is a dictionary of algorithms, each a byte sequence in
    base64 between colons: ``sha-256=:...:, sha-512=:...:``. Only
    ``sha-256`` is read; a malformed one declares nothing.
    """
    for member in field.split(","):
        key, _, value = member.strip().partition("=")
        if key.strip() != "sha-256":
            continue
        item = value.partition(";")[0].strip()  # Its parameters, if any, left out.
        if len(item) < 2 or item[0] != ":" or item[-1] != ":":
            return None
        try:
            digest = base64.b64decode(item[1:-1], validate=True)
        except ValueError:
            return None
        return digest if len(digest) == hashlib.sha256().digest_size else None
    return None


def _target(request: Request) -> str:
    """The request's path and query, as sent."""
    target = request.scope.get("raw_path", b"").decode("latin-1") or request.url.path
    if query := request.scope.get("query_string", b""):
        target = f"{target}?{query.decode('latin-1')}"
    return target


async def proves_key(request: Request, algorithm: int, key: bytes) -> bool:
    """Whether ``request`` is signed by ``key``, now (``DEVICES.SIGNATURE_WINDOW``).

    For a key that is not bound yet: the caller holds what it asks to
    bind. The request carries ``X-Device-Time`` and ``X-Device-Signature``,
    and no device id.
    """
    signed = await read_signature(request)
    if signed is None:
        return False
    _, signature, message = signed
    return verify_signature(algorithm, key, signature, message)


async def bind_key(
    uow: DevicesUnitOfWork, user_id: UUID, name: str, algorithm: int, key: bytes
) -> BoundDevice:
    """Bind ``key`` to the user in ``uow``, which its caller commits.

    Under the user's lock, so two binds at once count each other.

    Raises:
        TooManyKeys: The user holds ``DEVICES.MAX_DEVICES`` already.
    """
    await uow.tokens.lock_user(user_id)
    limit = registry.settings(DevicesSettings).MAX_DEVICES
    if await uow.devices.count(user_id) >= limit:
        raise TooManyKeys("Unbind a device before binding another.")
    device = await uow.devices.add(user_id, name, algorithm, key)
    stored = await uow.users.get(UserId(user_id))
    stored.record(DeviceBound(user_id=user_id, device_id=device.id))
    return device


async def _check(connection: HTTPConnection, user: Any) -> DeviceProof | None:  # noqa: ANN401
    """What the device signed, if every part holds; its time recorded.

    The device is looked up from the headers, and one that is not the
    user's refused, before a byte of the body is read. With a declared
    digest that the signature holds for, the body is read only then, and
    must be the one declared; without one (or one the signature does not
    hold for, unless ``CONTENT_DIGEST`` requires it), the signature is
    checked once the body is read. No unit of work is open while the
    body arrives: the device is read again in the one that records the
    request, and refused if it was unbound, or its key changed, meanwhile.
    """
    if not isinstance(connection, Request) or not getattr(
        user, "is_authenticated", False
    ):
        return None
    try:
        device_id = UUID(connection.headers.get(DEVICE_ID, ""))
    except ValueError:
        return None
    sent = _sent(connection)
    config = registry.settings(DevicesSettings)
    if sent is None or (sent[2] is None and config.CONTENT_DIGEST == "required"):
        return None
    time, signature, declared = sent
    async with DevicesUnitOfWork() as uow:
        device = await uow.devices.get(device_id)
    if device is None or device.user_id != user.id:
        return None
    message = await _signed(connection, device, time, signature, declared, config)
    if message is None:
        return None
    digest = hashlib.sha256(message).hexdigest()
    cached = config.SIGNATURE_STORE == "cache"
    now = utc_now()
    async with DevicesUnitOfWork() as uow:
        current = await uow.devices.get(device.id)  # Unbound while the body came?
        if current is None or _key_of(current) != _key_of(device):
            return None
        if cached:
            if not await _first_in_cache(device.id, digest, config.SIGNATURE_WINDOW):
                return None
        elif not await uow.devices.use(device.id, digest, time):
            return None
        last = current.last_used_at
        stale = last is None or now - last > TOUCH_EVERY
        if stale:
            window = int(config.SIGNATURE_WINDOW.total_seconds() * 1000)
            await uow.devices.touch(
                device.id, time, int(now.timestamp() * 1000) - window
            )
        if stale or not cached:
            await uow.commit()
    return DeviceProof(
        device=current,
        signed=message,
        signature=signature,
        signed_at=datetime.fromtimestamp(time / 1000, UTC),
    )


def _key_of(device: BoundDevice) -> tuple[UUID, int, bytes]:
    """Whose ``device`` is, and the key it signs with: what checks its signature."""
    return device.user_id, device.algorithm, device.public_key


async def _signed(
    request: Request,
    device: BoundDevice,
    time: int,
    signature: bytes,
    declared: bytes | None,
    config: DevicesSettings,
) -> bytes | None:
    """The message ``device`` signed for ``request``, or None if it signed none.

    A declared digest the signature holds for is checked before the body
    is read, and the body against it after.
    """
    target = _target(request)
    if declared is not None:
        message = _message(request.method, target, time, declared)
        if verify_signature(device.algorithm, device.public_key, signature, message):
            body = await request.body()
            return message if hashlib.sha256(body).digest() == declared else None
        if config.CONTENT_DIGEST == "required":
            return None
    message = signed_message(request.method, target, time, await request.body())
    if verify_signature(device.algorithm, device.public_key, signature, message):
        return message
    return None


async def _first_in_cache(device_id: UUID, digest: str, window: timedelta) -> bool:
    """Whether the request ``digest`` is new to the default cache, which keeps it.

    Kept twice the window: a request signed at the window's far edge is
    refused for its time after that. False while the cache is down: a
    request nothing can remember is not let through.
    """
    try:
        return await caches["default"].add(
            f"domainkit.devices.signature:{device_id}:{digest}", 1, ttl=window * 2
        )
    except CacheError:
        logger.exception(
            "DEVICES.SIGNATURE_STORE is the cache, which is down: a signed "
            "request was refused."
        )
        return False
