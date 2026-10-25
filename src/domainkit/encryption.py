"""Secrets the database keeps, encrypted: AES-256-GCM under a key from ``SECRET_KEY``.

Some secrets must be read back, so they cannot be hashed like passwords:
TOTP secrets (``domainkit.contrib.auth.mfa``), signing secrets shared with
other services. ``SecretBox(purpose)`` encrypts them under a key derived
for its purpose (HKDF-SHA256, ``domainkit.signing.derive_key``), so the
database alone reveals nothing, and binds each value to what it belongs
to (its associated data, such as the owner's id), so a value moved to
another row does not decrypt::

    box = SecretBox("billing.webhook_secret")
    sealed = box.encrypt(secret, associated=subscription_id.bytes)
    secret, current = box.decrypt(sealed, associated=subscription_id.bytes)
    if not current:
        ...  # Store box.encrypt(secret, ...) again, under the current key.

A value is a version byte, a random 96-bit nonce, and the ciphertext with
its tag. Values encrypted under a key that is now in
``SECRET_KEY_FALLBACKS`` still decrypt, and ``decrypt`` says so, for the
caller to encrypt them again. Needs ``cryptography``:
``pip install "domainkit[mfa]"``. See ADR 0030.
"""

import secrets
from typing import Final

from domainkit.exceptions import ImproperlyConfigured
from domainkit.signing import derive_keys

__all__ = [
    "DecryptionFailed",
    "SecretBox",
]

VERSION: Final = b"\x01"
"""AES-256-GCM, a 96-bit nonce, and a key derived from ``SECRET_KEY``."""
NONCE_BYTES: Final = 12


class DecryptionFailed(Exception):
    """No key decrypts the value: wrong key, other associated data, or tampering."""


class SecretBox:
    """Encrypts and decrypts values for one purpose."""

    __slots__ = ("_ciphers", "purpose")

    def __init__(self, purpose: str) -> None:
        """Derive the keys of ``purpose`` from ``SECRET_KEY`` and its fallbacks.

        Raises:
            ImproperlyConfigured: ``cryptography`` is missing, or no
                ``SECRET_KEY`` is set.
        """
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:
            raise ImproperlyConfigured(
                'Encrypting secrets needs cryptography: pip install "domainkit[mfa]".'
            ) from exc
        self.purpose = purpose
        self._ciphers = tuple(
            AESGCM(key) for key in derive_keys(f"encryption:{purpose}")
        )

    def __repr__(self) -> str:
        """Name the purpose, never a key."""
        return f"SecretBox(purpose={self.purpose!r})"

    def encrypt(self, plaintext: bytes, *, associated: bytes) -> bytes:
        """``plaintext``, encrypted under the current key, bound to ``associated``."""
        nonce = secrets.token_bytes(NONCE_BYTES)
        sealed = self._ciphers[0].encrypt(nonce, plaintext, VERSION + associated)
        return VERSION + nonce + sealed

    def decrypt(self, value: bytes, *, associated: bytes) -> tuple[bytes, bool]:
        """The plaintext, and whether ``SECRET_KEY`` (not a fallback) sealed it.

        Raises:
            DecryptionFailed: No key decrypts it with ``associated``.
        """
        from cryptography.exceptions import InvalidTag

        version, nonce = value[:1], value[1 : 1 + NONCE_BYTES]
        sealed = value[1 + NONCE_BYTES :]
        if version != VERSION or len(nonce) != NONCE_BYTES:
            raise DecryptionFailed("The value is not one SecretBox encrypted.")
        for index, cipher in enumerate(self._ciphers):
            try:
                plaintext = cipher.decrypt(nonce, sealed, VERSION + associated)
            except InvalidTag:
                continue
            return plaintext, index == 0
        raise DecryptionFailed("No key decrypts the value.")
