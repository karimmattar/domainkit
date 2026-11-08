"""Passkeys (WebAuthn) and bound devices: keys a user's devices hold.

Install it after ``domainkit.contrib.auth``, set the relying party, and
mount its routes::

    INSTALLED_DOMAINS = [..., "domainkit.contrib.auth", "domainkit.contrib.devices"]
    DEVICES = {
        "RP_ID": "example.com",
        "ORIGINS": ("https://app.example.com",),
    }

A passkey logs a user in with no password and no second factor (it is
both), and steps them up. A device a mobile application binds signs
requests with a key it never lets out (``DeviceSigned``), and steps its
user up. See ``docs/auth/devices.md``.
"""

from .binding import DeviceSigned
from .exceptions import (
    DeviceAlreadyBound,
    DeviceLoginOff,
    DeviceNotFound,
    DeviceSignatureRefused,
    EnrollmentOff,
    EnrollmentRefused,
    PasskeyNotFound,
    PasskeyRefused,
    PasskeysOff,
    ReauthenticationRequired,
    SecondFactorRequired,
    TooManyKeys,
    UnsupportedKey,
)
from .stores import BoundDevice, DevicesUnitOfWork, Passkey

__all__ = [
    "BoundDevice",
    "DeviceAlreadyBound",
    "DeviceLoginOff",
    "DeviceNotFound",
    "DeviceSignatureRefused",
    "DeviceSigned",
    "DevicesUnitOfWork",
    "EnrollmentOff",
    "EnrollmentRefused",
    "Passkey",
    "PasskeyNotFound",
    "PasskeyRefused",
    "PasskeysOff",
    "ReauthenticationRequired",
    "SecondFactorRequired",
    "TooManyKeys",
    "UnsupportedKey",
]
