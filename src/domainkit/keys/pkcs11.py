"""A hardware security module, through PKCS#11, as a ``KeyProvider``.

::

    provider = Pkcs11KeyProvider(
        "/usr/lib/softhsm/libsofthsm2.so", token_label="app",
        pin=env.str("HSM_PIN"), key_label="kek-2026",
        retired_labels=("kek-2025",),
    )

The key-encryption key is an AES-256 key inside the HSM, found by its
label; it never leaves it. A data key is 32 random bytes made here,
encrypted by the HSM with AES-GCM (a random 96-bit nonce, the key's label
as additional data), and decrypted there. ``rewrap`` moves a data key to
``key_label``, from any of ``retired_labels``: rotate by making a new
key in the HSM, naming it ``key_label`` and the old one retired, and
running ``manage.py keys rewrap``. A wrapped key is ``pkcs11:``, the
label, and the nonce and ciphertext in URL-safe base64.

The PKCS#11 library is called on a worker thread, one session at a
time (``python-pkcs11``, the ``pkcs11`` extra).
"""

import asyncio
import secrets
import threading
from collections.abc import Sequence
from typing import Any, Final

from . import (
    DATA_KEY_BYTES,
    NONCE_BYTES,
    DataKey,
    KeyProvider,
    KeyProviderError,
    UnknownKey,
    _b64,
    _unb64,
)

__all__ = ["Pkcs11KeyProvider"]

NAME: Final = "pkcs11"
_AAD: Final = b"domainkit.keys.pkcs11\x00"


class Pkcs11KeyProvider(KeyProvider):
    """Data keys under an AES key inside an HSM (see the module)."""

    name = NAME

    def __init__(
        self,
        library: str,
        *,
        token_label: str,
        pin: str,
        key_label: str,
        retired_labels: Sequence[str] = (),
    ) -> None:
        """Use the key ``key_label`` of the token ``token_label`` of ``library``.

        Raises:
            ValueError: A label is empty, has a ``:``, or is named twice.
        """
        labels = (key_label, *retired_labels)
        if len(set(labels)) != len(labels) or any(
            not label or ":" in label for label in labels
        ):
            raise ValueError(
                "PKCS#11 key labels are distinct, not empty, and without ':'."
            )
        self.library = library
        self.token_label = token_label
        self.key_label = key_label
        self.labels = frozenset(labels)
        self._pin = pin
        self._lock = threading.Lock()
        self._session: Any = None

    def __repr__(self) -> str:
        """The token and key; never the PIN."""
        return (
            f"Pkcs11KeyProvider(token_label={self.token_label!r}, "
            f"key_label={self.key_label!r})"
        )

    async def generate_data_key(self) -> DataKey:
        """32 random bytes, and them encrypted by the HSM."""
        plaintext = secrets.token_bytes(DATA_KEY_BYTES)
        return DataKey(plaintext, await asyncio.to_thread(self._wrap, plaintext))

    async def unwrap(self, wrapped: str) -> bytes:
        """The data key ``wrapped`` holds, decrypted by the HSM."""
        return await asyncio.to_thread(self._unwrap, wrapped)

    async def rewrap(self, wrapped: str) -> str:
        """``wrapped`` under ``key_label``."""
        return await asyncio.to_thread(lambda: self._wrap(self._unwrap(wrapped)))

    def _wrap(self, plaintext: bytes) -> str:
        """``pkcs11:<label>:<nonce + ciphertext>`` (blocking)."""
        nonce = secrets.token_bytes(NONCE_BYTES)
        sealed = self._crypt("encrypt", self.key_label, nonce, plaintext)
        return f"{NAME}:{self.key_label}:{_b64(nonce + sealed)}"

    def _unwrap(self, wrapped: str) -> bytes:
        """What ``_wrap`` wrapped (blocking).

        Raises:
            UnknownKey: Another's key, a label not named here, or changed.
        """
        provider, _, rest = wrapped.partition(":")
        label, _, payload = rest.partition(":")
        if provider != NAME or label not in self.labels:
            raise UnknownKey("This HSM key did not wrap that key.")
        try:
            data = _unb64(payload)
        except ValueError:
            raise UnknownKey("The wrapped key was changed, or cut short.") from None
        return self._crypt("decrypt", label, data[:NONCE_BYTES], data[NONCE_BYTES:])

    def _crypt(self, action: str, label: str, nonce: bytes, data: bytes) -> bytes:
        """Encrypt or decrypt ``data`` with the HSM's key ``label`` (blocking).

        Raises:
            UnknownKey: The HSM refused to decrypt (the data was changed).
            KeyProviderError: The library, the token, or the key failed.
        """
        import pkcs11
        from pkcs11 import KeyType, Mechanism, ObjectClass
        from pkcs11.mechanisms import GCMParams

        with self._lock:
            try:
                session = self._open()
                key = session.get_key(
                    label=label,
                    key_type=KeyType.AES,
                    object_class=ObjectClass.SECRET_KEY,
                )
            except RuntimeError as exc:  # PKCS11Error, or the library missing.
                self._session = None
                raise KeyProviderError(
                    f"The HSM's key could not be found ({type(exc).__name__})."
                ) from None
            params = GCMParams(nonce, _AAD + label.encode(), 128)
            try:
                result: bytes = getattr(key, action)(
                    data, mechanism=Mechanism.AES_GCM, mechanism_param=params
                )
            except pkcs11.exceptions.PKCS11Error as exc:
                if action == "decrypt":
                    raise UnknownKey("The HSM could not unwrap that key.") from None
                raise KeyProviderError(
                    f"The HSM failed to encrypt ({type(exc).__name__})."
                ) from None
        return result

    def _open(self) -> Any:  # noqa: ANN401 - python-pkcs11's Session.
        """The session, opened and logged in on first use."""
        if self._session is None:
            import pkcs11

            token = pkcs11.lib(self.library).get_token(token_label=self.token_label)
            try:
                self._session = token.open(user_pin=self._pin)
            except pkcs11.exceptions.UserAlreadyLoggedIn:
                # PKCS#11 logs in per process: another session of it did.
                self._session = token.open()
        return self._session
