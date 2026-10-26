"""Signed values: proof that this service made a value, and when (Django's ``signing``).

``SECRET_KEY`` signs. Keys in ``SECRET_KEY_FALLBACKS`` still verify, so a
key can be replaced without breaking what the old one signed::

    from domainkit import signing

    signer = signing.Signer(salt="newsletter.unsubscribe")
    value = signer.sign("42")        # "42:<signature>"
    signer.unsign(value)             # "42"; BadSignature if anything changed

    token = signing.dumps({"invite": 7}, salt="invites")
    signing.loads(token, salt="invites", max_age=timedelta(days=7))

A signature is HMAC-SHA256 under a key derived for its ``salt`` (and for
plain or timestamped values), so a value signed for one purpose never
verifies for another. Signatures are compared in constant time. Signed
values are not secret: anyone can read them. ``derive_key`` gives other
uses, such as encryption, a key of their own from ``SECRET_KEY``.
"""

import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Sequence
from datetime import timedelta
from typing import Any, ClassVar, Final

from domainkit import clock
from domainkit.conf import settings
from domainkit.exceptions import DomainkitError, ImproperlyConfigured

__all__ = [
    "BadSignature",
    "Key",
    "SignatureExpired",
    "Signer",
    "TimestampSigner",
    "constant_time_compare",
    "derive_key",
    "derive_keys",
    "dumps",
    "loads",
]

DEFAULT_SALT: Final = "domainkit.signing"
"""The salt of ``dumps`` and ``loads`` when none is given."""
SEPARATOR: Final = ":"
"""Between a value, its timestamp, and its signature."""
DIGEST_SIZE: Final = hashlib.sha256().digest_size
_BASE62: Final = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

type Key = str | bytes
"""A secret key: text (as settings hold it) or bytes."""


class BadSignature(DomainkitError):
    """The value was not signed with this key and salt, or it was changed."""


class SignatureExpired(BadSignature):
    """The signature holds, but it is older than ``max_age``."""


def secret_keys() -> tuple[bytes, ...]:
    """``SECRET_KEY``, then each of ``SECRET_KEY_FALLBACKS``, as bytes.

    Raises:
        ImproperlyConfigured: ``SECRET_KEY`` is not set.
    """
    if settings.SECRET_KEY is None:
        raise ImproperlyConfigured(
            "Set SECRET_KEY: it signs the values this service hands out "
            "(docs/reference.md)."
        )
    keys = (settings.SECRET_KEY, *settings.SECRET_KEY_FALLBACKS)
    return tuple(key.get_secret_value().encode() for key in keys)


def hkdf(key: bytes, *, info: bytes, length: int = DIGEST_SIZE) -> bytes:
    """HKDF-SHA256 (RFC 5869) without a salt: ``length`` bytes for ``info``.

    Raises:
        ValueError: ``length`` is not between 1 and 8160 bytes.
    """
    if not 0 < length <= 255 * DIGEST_SIZE:
        raise ValueError(f"HKDF makes 1 to {255 * DIGEST_SIZE} bytes, not {length}.")
    prk = hmac.new(bytes(DIGEST_SIZE), key, hashlib.sha256).digest()
    blocks = [b""]
    while len(blocks) - 1 < -(-length // DIGEST_SIZE):
        counter = bytes([len(blocks)])
        blocks.append(
            hmac.new(prk, blocks[-1] + info + counter, hashlib.sha256).digest()
        )
    return b"".join(blocks)[:length]


def derive_key(purpose: str, *, key: Key | None = None, length: int = 32) -> bytes:
    """A key for ``purpose`` alone, derived from ``key`` (default: ``SECRET_KEY``).

    Keys for different purposes are unrelated: one never reveals another,
    nor the secret they come from.
    """
    secret = secret_keys()[0] if key is None else _key(key)
    return hkdf(secret, info=f"domainkit:{purpose}".encode(), length=length)


def derive_keys(purpose: str, *, length: int = 32) -> tuple[bytes, ...]:
    """``derive_key`` for ``SECRET_KEY`` and each fallback, in that order."""
    return tuple(derive_key(purpose, key=key, length=length) for key in secret_keys())


def constant_time_compare(first: str | bytes, second: str | bytes) -> bool:
    """Whether two values are equal, in a time that does not tell how close."""
    return hmac.compare_digest(_key(first, empty=True), _key(second, empty=True))


class Signer:
    """Signs text, and checks the signature: ``value:signature``.

    It signs with ``key`` (default: ``SECRET_KEY``, which brings
    ``SECRET_KEY_FALLBACKS`` along) and also accepts ``fallback_keys``.
    """

    kind: ClassVar[str] = "plain"
    """Part of every derived key: plain and timestamped values never mix."""

    __slots__ = ("_keys", "salt")

    def __init__(
        self,
        *,
        salt: str = DEFAULT_SALT,
        key: Key | None = None,
        fallback_keys: Sequence[Key] = (),
    ) -> None:
        """Sign for ``salt``: values signed for another salt never verify."""
        keys = (*(secret_keys() if key is None else (key,)), *fallback_keys)
        self.salt = salt
        self._keys = tuple(
            derive_key(f"signing:{self.kind}:{salt}", key=secret) for secret in keys
        )

    def __repr__(self) -> str:
        """Name the salt, never the keys."""
        return f"{type(self).__name__}(salt={self.salt!r})"

    def signature(self, value: str) -> str:
        """The signature of ``value`` under the signing key."""
        return _signature(self._keys[0], value)

    def sign(self, value: str) -> str:
        """``value``, a separator, and its signature."""
        return f"{value}{SEPARATOR}{self.signature(value)}"

    def unsign(self, signed_value: str) -> str:
        """The value ``sign`` signed.

        Raises:
            BadSignature: No key and salt signed it, or it was changed.
        """
        value, separator, signature = signed_value.rpartition(SEPARATOR)
        if not separator:
            raise BadSignature("No signature found.")
        for key in self._keys:
            if constant_time_compare(_signature(key, value), signature):
                return value
        raise BadSignature("The signature does not match.")

    def sign_object(self, obj: object) -> str:
        """``obj`` as compact JSON, in URL-safe base64, signed."""
        return self.sign(_encode(obj))

    def unsign_object(self, signed_value: str) -> Any:  # noqa: ANN401 - any JSON value.
        """The object ``sign_object`` signed.

        Raises:
            BadSignature: The signature does not hold.
        """
        return _decode(self.unsign(signed_value))


class TimestampSigner(Signer):
    """Signs text with the time it was signed: ``value:timestamp:signature``.

    ``unsign`` refuses signatures older than ``max_age``.
    """

    kind: ClassVar[str] = "timestamped"

    __slots__ = ()

    def timestamp(self) -> str:
        """The current time, in whole seconds, in base 62."""
        return _base62(int(_now()))

    def sign(self, value: str) -> str:
        """``value``, the time, and the signature of both."""
        return super().sign(f"{value}{SEPARATOR}{self.timestamp()}")

    def unsign(
        self, signed_value: str, max_age: float | timedelta | None = None
    ) -> str:
        """The value ``sign`` signed, if signed at most ``max_age`` ago.

        Raises:
            BadSignature: The signature does not hold.
            SignatureExpired: It holds, but it is older than ``max_age``.
        """
        value, _, timestamp = super().unsign(signed_value).rpartition(SEPARATOR)
        age = _now() - _from_base62(timestamp)
        if max_age is not None:
            seconds = (
                max_age.total_seconds() if isinstance(max_age, timedelta) else max_age
            )
            if age > seconds:
                raise SignatureExpired(f"The signature is older than {seconds:g}s.")
        return value

    def unsign_object(
        self, signed_value: str, max_age: float | timedelta | None = None
    ) -> Any:  # noqa: ANN401 - any JSON value.
        """The object ``sign_object`` signed, if signed at most ``max_age`` ago.

        Raises:
            BadSignature: The signature does not hold.
            SignatureExpired: It holds, but it is older than ``max_age``.
        """
        return _decode(self.unsign(signed_value, max_age))


def dumps(obj: object, *, salt: str = DEFAULT_SALT, key: Key | None = None) -> str:
    """``obj`` as signed, timestamped, URL-safe JSON (``loads`` reads it)."""
    return TimestampSigner(salt=salt, key=key).sign_object(obj)


def loads(
    signed_value: str,
    *,
    salt: str = DEFAULT_SALT,
    key: Key | None = None,
    fallback_keys: Sequence[Key] = (),
    max_age: float | timedelta | None = None,
) -> Any:  # noqa: ANN401 - any JSON value.
    """The object ``dumps`` signed, if signed at most ``max_age`` ago.

    Raises:
        BadSignature: The signature does not hold.
        SignatureExpired: It holds, but it is older than ``max_age``.
    """
    signer = TimestampSigner(salt=salt, key=key, fallback_keys=fallback_keys)
    return signer.unsign_object(signed_value, max_age)


def _now() -> float:
    """Seconds since the epoch, on ``domainkit.clock``."""
    return clock.now().timestamp()


def _key(value: Key, *, empty: bool = False) -> bytes:
    """``value`` as bytes; text is UTF-8.

    Raises:
        ValueError: ``value`` is empty, and ``empty`` does not allow it.
    """
    data = value.encode() if isinstance(value, str) else value
    if not (data or empty):
        raise ValueError("A key cannot be empty.")
    return data


def _signature(key: bytes, value: str) -> str:
    """HMAC-SHA256 of ``value`` under ``key``, in URL-safe base64."""
    return _b64encode(hmac.new(key, value.encode(), hashlib.sha256).digest())


def _b64encode(data: bytes) -> str:
    """URL-safe base64 without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _encode(obj: object) -> str:
    """``obj`` as compact JSON, in URL-safe base64."""
    text = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    return _b64encode(text.encode())


def _decode(data: str) -> Any:  # noqa: ANN401 - any JSON value.
    """The object ``_encode`` encoded.

    Raises:
        BadSignature: ``data`` is no such object.
    """
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
        return json.loads(raw)
    except (binascii.Error, ValueError):
        raise BadSignature("The signed value is not an encoded object.") from None


def _base62(number: int) -> str:
    """``number`` (zero or more) in base 62."""
    digits = ""
    while True:
        number, digit = divmod(number, 62)
        digits = _BASE62[digit] + digits
        if not number:
            return digits


def _from_base62(text: str) -> int:
    """The number ``_base62`` wrote.

    Raises:
        BadSignature: ``text`` is not one.
    """
    if not text:
        raise BadSignature("No timestamp found.")
    number = 0
    for char in text:
        digit = _BASE62.find(char)
        if digit < 0:
            raise BadSignature("The timestamp is not base 62.")
        number = number * 62 + digit
    return number
