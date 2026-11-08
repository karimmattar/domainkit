"""What a passkey, or a bound device, may be refused."""

from fastapi import status

from domainkit.contrib.auth.integration import ReauthenticationRequired
from domainkit.ddd import BusinessRuleViolation, Conflict, NotFound
from domainkit.http.errors import APIError

__all__ = [
    "DeviceAlreadyBound",
    "DeviceLoginOff",
    "DeviceNotFound",
    "DeviceSignatureRefused",
    "EnrollmentOff",
    "EnrollmentRefused",
    "PasskeyNotFound",
    "PasskeyRefused",
    "PasskeysOff",
    "ReauthenticationRequired",
    "SecondFactorRequired",
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


class EnrollmentRefused(APIError):
    """The code, the password, or the key's signature does not hold (401)."""

    def __init__(self) -> None:
        """Use status 401 and the code ``enrollment_refused``: never which."""
        super().__init__(
            status.HTTP_401_UNAUTHORIZED,
            "The code, the password, or the device's signature is wrong.",
            code="enrollment_refused",
        )


class SecondFactorRequired(APIError):
    """The user has a second factor: enrollment needs its code too (401)."""

    def __init__(self) -> None:
        """Use status 401 and the code ``second_factor_required``."""
        super().__init__(
            status.HTTP_401_UNAUTHORIZED,
            "Your account has a second factor: send a code from it too.",
            code="second_factor_required",
        )


class DeviceLoginOff(NotFound):
    """``DEVICES.DEVICE_LOGIN`` is off: devices do not log in here (404)."""

    code = "device_login_off"


class EnrollmentOff(NotFound):
    """``DEVICES.ENROLLMENT`` is off: devices do not enroll here (404)."""

    code = "enrollment_off"


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


class DeviceAlreadyBound(Conflict):
    """The user has a bound device: approve the new one from it (409)."""

    code = "device_already_bound"


class UnsupportedKey(BusinessRuleViolation):
    """Not a P-256 or Ed25519 public key (SubjectPublicKeyInfo, base64url)."""

    code = "unsupported_device_key"
