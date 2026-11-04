"""What a passkey, or a bound device, may be refused."""

from fastapi import status

from domainkit.contrib.auth.integration import ReauthenticationRequired
from domainkit.ddd import BusinessRuleViolation, Conflict, NotFound
from domainkit.http.errors import APIError

__all__ = [
    "DeviceNotFound",
    "DeviceSignatureRefused",
    "PasskeyNotFound",
    "PasskeyRefused",
    "PasskeysOff",
    "ReauthenticationRequired",
    "TooManyKeys",
    "UnsupportedKey",
]


class PasskeyRefused(APIError):
    """A passkey's answer does not hold (401); the reason is logged, not sent."""

    def __init__(self) -> None:
        """Use status 401 and the code ``passkey_refused``."""
        super().__init__(
            status.HTTP_401_UNAUTHORIZED,
            "The passkey could not be verified; try again.",
            code="passkey_refused",
        )


class DeviceSignatureRefused(APIError):
    """The request is not signed by a device of the user, now, once (401)."""

    def __init__(self) -> None:
        """Use status 401 and the code ``device_signature_refused``."""
        super().__init__(
            status.HTTP_401_UNAUTHORIZED,
            "Sign this request with this device's key.",
            code="device_signature_refused",
        )


class PasskeysOff(NotFound):
    """``DEVICES.RP_ID`` is not set: no passkeys here (404)."""

    code = "passkeys_off"


class PasskeyNotFound(NotFound):
    """The user has no passkey with that id."""

    code = "passkey_not_found"


class DeviceNotFound(NotFound):
    """The user has no device with that id."""

    code = "device_not_found"


class TooManyKeys(Conflict):
    """The user has ``MAX_PASSKEYS`` (or ``MAX_DEVICES``) already: remove one first."""

    code = "too_many_keys"


class UnsupportedKey(BusinessRuleViolation):
    """Not a P-256 or Ed25519 public key (SubjectPublicKeyInfo, base64url)."""

    code = "unsupported_device_key"
