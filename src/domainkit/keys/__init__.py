"""Keys for envelope encryption: data keys wrapped by a key-encryption key.

A value is encrypted under a data key (AES-256-GCM, in this process), and
the data key is stored only wrapped: encrypted under a key-encryption key
(KEK) that never leaves its key management service. Unwrapping a data key
is one call to the service; the process then keeps the data key in memory
and encrypts and decrypts without further calls. A ``KeyProvider`` is the
port to that service::

    provider = VaultTransitKeyProvider("https://vault:8200", "payments", token=...)
    made = await provider.generate_data_key()   # made.plaintext, made.wrapped
    key = await provider.unwrap(made.wrapped)   # The same 32 bytes.
    wrapped = await provider.rewrap(made.wrapped)  # Under the KEK's newest version.

Three adapters:

- ``LocalKeyProvider``: a KEK derived from ``SECRET_KEY`` (and its
  fallbacks), for development and tests; ``domainkit.contrib.keys`` refuses
  it in staging and production unless ``KEYS.ALLOW_LOCAL_KEK`` says so.
- ``domainkit.keys.aws.AwsKmsKeyProvider``: AWS KMS, signed with SigV4.
- ``domainkit.keys.vault.VaultTransitKeyProvider``: HashiCorp Vault's
  transit engine.

The cloud adapters call through ``domainkit.http.client`` (``domainkit[httpclient]``),
which logs a call's method, origin and path only: never a body, so never a
key. No adapter logs, repeats in an error, or ``repr``s key material, and
each is safe to share between the tasks of an event loop. A wrapped key is
text that names its provider (``local:``, ``aws-kms:``, ``vault:``), so a
key wrapped by one is never handed to another. See ADR 0119.
"""

import base64
import binascii
import hashlib
import secrets
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar, Final

from domainkit.exceptions import DomainkitError, ImproperlyConfigured

__all__ = [
    "DATA_KEY_BYTES",
    "DataKey",
    "KeyProvider",
    "KeyProviderError",
    "LocalKeyProvider",
    "UnknownKey",
]

DATA_KEY_BYTES: Final = 32
"""The size of a data key: AES-256."""
NONCE_BYTES: Final = 12
LOCAL: Final = "local"
_LOCAL_AAD: Final = b"domainkit.keys.local\x00"
_KEK_ID_DIGITS: Final = 8


class KeyProviderError(DomainkitError):
    """The key service failed, or refused: it said so, or could not be reached.

    The message names the provider and what failed, never a key or the
    service's answer, which may echo what it was sent.
    """


class UnknownKey(KeyProviderError):
    """A wrapped key this provider cannot unwrap: another's, damaged, or retired."""


@dataclass(frozen=True, slots=True, repr=False)
class DataKey:
    """A new data key: its 32 bytes, to use now, and its wrapped form, to store."""

    plaintext: bytes
    wrapped: str

    def __repr__(self) -> str:
        """Never the key: only that it is one."""
        return "DataKey(plaintext=<hidden>, wrapped=<hidden>)"


class KeyProvider(ABC):
    """Makes, unwraps, and rewraps data keys under a key-encryption key."""

    name: ClassVar[str]
    """The prefix of the keys it wraps: ``local``, ``aws-kms``, ``vault``."""

    @abstractmethod
    async def generate_data_key(self) -> DataKey:
        """A new random data key, and that key wrapped under the current KEK."""

    @abstractmethod
    async def unwrap(self, wrapped: str) -> bytes:
        """The data key ``wrapped`` holds.

        Raises:
            UnknownKey: The provider cannot unwrap it.
            KeyProviderError: The service failed.
        """

    @abstractmethod
    async def rewrap(self, wrapped: str) -> str:
        """``wrapped`` wrapped again under the KEK's newest version.

        The data key is unchanged, so what it encrypted stays readable;
        only the KEK version that wraps it moves on, after the KEK rotated.

        Raises:
            UnknownKey: The provider cannot unwrap it.
            KeyProviderError: The service failed.
        """

    def owns(self, wrapped: str) -> bool:
        """Whether ``wrapped`` names this provider as the one that wrapped it."""
        return wrapped.startswith(f"{self.name}:")

    def __repr__(self) -> str:
        """The provider's class, never a key or a credential."""
        return f"{type(self).__name__}()"


class LocalKeyProvider(KeyProvider):
    """A KEK held in this process: for development and tests only.

    The KEK is derived from ``SECRET_KEY`` (``derive_keys``), and each of
    ``SECRET_KEY_FALLBACKS`` gives an older KEK that still unwraps, so
    rotating ``SECRET_KEY`` rotates it. A wrapped key is ``local:``, the
    first 8 hex digits of the KEK's SHA-256 (its id), and the AES-256-GCM
    nonce and ciphertext in URL-safe base64.

    Anyone who reads ``SECRET_KEY`` can unwrap every data key: in staging
    and production use a key management service.
    """

    name = LOCAL

    __slots__ = ("_keks",)

    def __init__(self, keks: Sequence[bytes]) -> None:
        """Wrap under ``keks[0]``; unwrap under any of them.

        Raises:
            ImproperlyConfigured: ``cryptography`` is missing.
            ValueError: No KEK, or one that is not 32 bytes.
        """
        if not keks or any(len(kek) != DATA_KEY_BYTES for kek in keks):
            raise ValueError("LocalKeyProvider takes one or more KEKs of 32 bytes.")
        aead = _aesgcm()
        self._keks = {
            hashlib.sha256(kek).hexdigest()[:_KEK_ID_DIGITS]: aead(kek) for kek in keks
        }

    @classmethod
    def from_secret_key(cls) -> "LocalKeyProvider":
        """The KEKs derived from ``SECRET_KEY`` and each of its fallbacks."""
        from domainkit.signing import derive_keys

        return cls(derive_keys("keys.kek"))

    async def generate_data_key(self) -> DataKey:
        """A random data key, wrapped under the current KEK."""
        plaintext = secrets.token_bytes(DATA_KEY_BYTES)
        return DataKey(plaintext, self.wrap_now(plaintext))

    async def unwrap(self, wrapped: str) -> bytes:
        """The data key ``wrapped`` holds (see ``unwrap_now``)."""
        return self.unwrap_now(wrapped)

    async def rewrap(self, wrapped: str) -> str:
        """``wrapped`` under the current KEK."""
        return self.wrap_now(self.unwrap_now(wrapped))

    def wrap_now(self, plaintext: bytes) -> str:
        """``plaintext`` wrapped under the current KEK, without awaiting."""
        kek_id, cipher = next(iter(self._keks.items()))
        nonce = secrets.token_bytes(NONCE_BYTES)
        sealed = cipher.encrypt(nonce, plaintext, _LOCAL_AAD + kek_id.encode())
        return f"{LOCAL}:{kek_id}:{_b64(nonce + sealed)}"

    def unwrap_now(self, wrapped: str) -> bytes:
        """The data key ``wrapped`` holds, without awaiting: nothing leaves the process.

        Raises:
            UnknownKey: It is not a local key, its KEK is not one of these,
                or it was changed.
        """
        from cryptography.exceptions import InvalidTag

        provider, _, rest = wrapped.partition(":")
        kek_id, _, payload = rest.partition(":")
        cipher = self._keks.get(kek_id) if provider == LOCAL else None
        if cipher is None:
            raise UnknownKey(
                "The local provider did not wrap that key, or not under SECRET_KEY "
                "or one of SECRET_KEY_FALLBACKS."
            )
        try:
            data = _unb64(payload)
            return bytes(
                cipher.decrypt(
                    data[:NONCE_BYTES], data[NONCE_BYTES:], _LOCAL_AAD + kek_id.encode()
                )
            )
        except (ValueError, InvalidTag):
            raise UnknownKey("The wrapped key was changed, or cut short.") from None


def _aesgcm() -> type:
    """``cryptography``'s AES-GCM.

    Raises:
        ImproperlyConfigured: ``cryptography`` is missing.
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - only without the [mfa] extra.
        raise ImproperlyConfigured(
            'Envelope encryption needs cryptography: pip install "domainkit[mfa]".'
        ) from exc
    return AESGCM


def _b64(data: bytes) -> str:
    """URL-safe base64 without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    """What ``_b64`` wrote.

    Raises:
        ValueError: It is not URL-safe base64.
    """
    try:
        return base64.b64decode(text + "=" * (-len(text) % 4), b"-_", validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("not URL-safe base64") from None
