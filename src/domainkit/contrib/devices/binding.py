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
``DEVICES.SIGNATURE_WINDOW`` of the server's time, later than any request
it signed before: a signature works once, whichever process reads it.
Keys are P-256 (ES256, the signature DER as both platforms make it) or
Ed25519, sent as SubjectPublicKeyInfo or, for P-256, the raw point the
Secure Enclave exports.
"""

import base64
import hashlib
from typing import Any, Final
from uuid import UUID

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from starlette.requests import HTTPConnection, Request

from domainkit.apps import registry
from domainkit.contrib.auth.integration import utc_now
from domainkit.http.access import BasePermission

from .exceptions import DeviceSignatureRefused, UnsupportedKey
from .settings import DevicesSettings
from .stores import BoundDevice, DevicesUnitOfWork
from .webauthn import EDDSA, ES256, WebAuthnError, b64decode, verify_signature

__all__ = ["DeviceSigned", "device_key", "signed_message", "signing_device"]

DEVICE_ID: Final = "x-device-id"
DEVICE_TIME: Final = "x-device-time"
DEVICE_SIGNATURE: Final = "x-device-signature"
VERSION: Final = b"domainkit-device-v1"
SIGNING_DEVICE: Final = "domainkit.signing_device"
"""Where a request keeps the device that signed it, checked once."""
MAX_SIGNATURE: Final = 200
"""Base64url characters a signature may have (ES256's DER is at most 72 bytes)."""


def signed_message(method: str, target: str, time: int, body: bytes) -> bytes:
    """What a device signs for a request."""
    digest = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).rstrip(b"=")
    return b"\n".join(
        (VERSION, method.encode(), target.encode(), str(time).encode(), digest)
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
        connection.scope[SIGNING_DEVICE] = await _check(connection, user)
    device: BoundDevice | None = connection.scope[SIGNING_DEVICE]
    return device


async def _check(connection: HTTPConnection, user: Any) -> BoundDevice | None:  # noqa: ANN401
    """The device that signed, if every part holds; its time recorded."""
    if not isinstance(connection, Request) or not getattr(
        user, "is_authenticated", False
    ):
        return None
    headers = connection.headers
    try:
        device_id = UUID(headers.get(DEVICE_ID, ""))
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
    target = (
        connection.scope.get("raw_path", b"").decode("latin-1") or connection.url.path
    )
    if query := connection.scope.get("query_string", b""):
        target = f"{target}?{query.decode('latin-1')}"
    message = signed_message(connection.method, target, time, await connection.body())
    async with DevicesUnitOfWork() as uow:
        device = await uow.devices.get(device_id)
        if (
            device is None
            or device.user_id != user.id
            or not verify_signature(
                device.algorithm, device.public_key, signature, message
            )
            or not await uow.devices.advance(device.id, time)
        ):
            return None
        await uow.commit()
    return device
