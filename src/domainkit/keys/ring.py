"""The data keys a process encrypts values with, and the envelope they make.

A ``Keyring`` holds the data keys (``KEYS.DATA_KEYS``: id to wrapped key),
unwrapped once through its ``KeyProvider``, and the id of the current one,
which seals new values; the others still open what they sealed, until
``manage.py keys reencrypt`` moves it to the current key. A value sealed
for a person (a subject) is sealed under that person's own key instead,
made on first use, stored wrapped under the current data key
(``SubjectKeyStore``), and destroyed when the person is erased: the
values it sealed can then never be opened again (crypto-shredding).

A sealed value is text::

    dk1:k.2026-10:<nonce and ciphertext, URL-safe base64>     # a data key
    dk1:s.<32 hex digits>:<nonce and ciphertext>             # a subject's key

``dk1`` is the format, then the key that sealed it; AES-256-GCM
authenticates both and the value's context (its table, column and row,
``domainkit.db.encrypted``), so a value moved to another row or column,
or claiming another key, does not open. See ADR 0119 and ADR 0120.

Opening never blocks on the network once the ring is loaded (``load()``,
at startup). A ring used before, inside SQLAlchemy's async session (whose
events run in a greenlet), loads itself there; a local provider's keys
load anywhere.
"""

import hashlib
import hmac
import secrets
import threading
import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Final, Protocol
from weakref import WeakKeyDictionary

from sqlalchemy import Connection
from sqlalchemy.engine import Transaction

from domainkit.encryption import DecryptionFailed
from domainkit.exceptions import ImproperlyConfigured
from domainkit.keys import (
    DATA_KEY_BYTES,
    NONCE_BYTES,
    KeyProvider,
    LocalKeyProvider,
    _aesgcm,
    _b64,
    _unb64,
)
from domainkit.signing import hkdf

__all__ = [
    "FORMAT",
    "Keyring",
    "KeysNotLoaded",
    "StoredSubjectKey",
    "SubjectKeyStore",
    "forget_held",
    "keyring",
    "sealed_key",
    "use_keyring",
]

FORMAT: Final = "dk1"
"""The envelope's format: AES-256-GCM, a 96-bit nonce, the key named."""
DATA: Final = "k"
"""Sealed under a data key (``dk1:k.<id>:``)."""
SUBJECT: Final = "s"
"""Sealed under a subject's key (``dk1:s.<id>:``)."""
INDEX_DIGITS: Final = 32
"""Hex digits of the HMAC kept in a blind index (128 bits)."""
_SUBJECT_AAD: Final = b"dk1.subject\x00"
_INDEX_INFO: Final = b"domainkit:keys.blind-index"
_INDEX_AAD: Final = b"dk1.index\x00"


class KeysNotLoaded(ImproperlyConfigured):
    """The ring's keys are not unwrapped, and cannot be here: ``await ring.load()``."""


@dataclass(frozen=True, slots=True, repr=False)
class StoredSubjectKey:
    """A subject's key as stored: wrapped under the data key ``wrapping_key``."""

    id: str
    subject: str
    wrapping_key: str
    wrapped: bytes

    def __repr__(self) -> str:
        """The ids, never the key."""
        return f"StoredSubjectKey(id={self.id!r}, wrapping_key={self.wrapping_key!r})"


class SubjectKeyStore(Protocol):
    """Where subjects' keys are kept, read on the transaction's own connection."""

    def find(self, connection: Connection, key_id: str) -> StoredSubjectKey | None:
        """The key ``key_id``; None once destroyed."""

    def of_subject(
        self, connection: Connection, subject: str
    ) -> StoredSubjectKey | None:
        """The key of ``subject``, if it has one."""

    def add(self, connection: Connection, key: StoredSubjectKey) -> StoredSubjectKey:
        """Store ``key``, unless the subject has one: the one stored wins."""


def sealed_key(text: str) -> tuple[str, str] | None:
    """The kind (``k`` or ``s``) and id of the key that sealed ``text``, if sealed."""
    form, _, rest = text.partition(":")
    reference, _, payload = rest.partition(":")
    kind, _, key_id = reference.partition(".")
    if form != FORMAT or kind not in {DATA, SUBJECT} or not key_id or not payload:
        return None
    return kind, key_id


_held: WeakKeyDictionary[Transaction, dict[str, tuple[str, str, bytes]]] = (
    WeakKeyDictionary()
)
"""Subjects' keys each open transaction read, by subject and by ``#<id>``:
the subject, the key's id, and the key.

Kept for the transaction only: a key destroyed elsewhere is never used by
a transaction that starts afterwards. Read and changed under ``_held_lock``.
"""
_held_lock: Final = threading.Lock()


class Keyring:
    """Data keys by id, the current one, and the subjects' keys (see the module)."""

    def __init__(
        self,
        provider: KeyProvider,
        data_keys: Mapping[str, str],
        current: str,
        *,
        known: Mapping[str, bytes] | None = None,
        subject_keys: SubjectKeyStore | None = None,
    ) -> None:
        """Seal under ``current``; open under any key of ``data_keys`` or ``known``.

        ``data_keys`` maps ids to keys wrapped by ``provider``; ``known``
        maps ids to keys already unwrapped (a development key).
        ``subject_keys`` keeps subjects' keys; without it, sealing for a
        subject is refused.

        Raises:
            ImproperlyConfigured: ``current`` is not one of the keys.
        """
        self.provider = provider
        self.data_keys = dict(data_keys)
        self.current = current
        self.subject_keys = subject_keys
        self._known = dict(known or {})
        self._keys: dict[str, bytes] | None = None
        ids = {*self.data_keys, *self._known}
        if current not in ids:
            raise ImproperlyConfigured(
                f"The current data key {current!r} is not one of the ring's keys."
            )
        self.ids = (current, *sorted(ids - {current}))
        """Every data key's id, the current one first."""

    def __repr__(self) -> str:
        """The ids and the provider, never a key."""
        return (
            f"Keyring(current={self.current!r}, ids={self.ids!r}, "
            f"provider={self.provider!r})"
        )

    @property
    def loaded(self) -> bool:
        """Whether the data keys are unwrapped."""
        return self._keys is not None

    async def load(self) -> None:
        """Unwrap every data key through the provider, once.

        Raises:
            KeyProviderError: The provider could not unwrap one.
        """
        if self._keys is not None:
            return
        keys = dict(self._known)
        for key_id, wrapped in self.data_keys.items():
            keys[key_id] = _checked(key_id, await self.provider.unwrap(wrapped))
        self._keys = keys

    def _load_now(self) -> dict[str, bytes]:
        """Unwrap the data keys where this code runs, or say why it cannot.

        A local provider unwraps in the process; any other only inside
        SQLAlchemy's async session or connection, whose greenlet can wait
        on the provider's call.

        Raises:
            KeysNotLoaded: Neither applies.
        """
        provider = self.provider
        if isinstance(provider, LocalKeyProvider):
            keys = dict(self._known)
            for key_id, wrapped in self.data_keys.items():
                keys[key_id] = _checked(key_id, provider.unwrap_now(wrapped))
            self._keys = keys
            return keys
        from sqlalchemy.exc import MissingGreenlet
        from sqlalchemy.util import await_only

        loading = self.load()
        try:
            await_only(loading)
        except MissingGreenlet:
            loading.close()
            raise KeysNotLoaded(
                "The data keys are not unwrapped yet, and the key service cannot "
                "be called here: await the ring's load() first (startup does)."
            ) from None
        return self._keys or {}

    def data_key(self, key_id: str) -> bytes:
        """The data key ``key_id``, unwrapped.

        Raises:
            DecryptionFailed: The ring holds no key ``key_id``.
            KeysNotLoaded: The keys are not unwrapped, and cannot be here.
        """
        keys = self._keys if self._keys is not None else self._load_now()
        try:
            return keys[key_id]
        except KeyError:
            raise DecryptionFailed(
                f"The value was sealed under the data key {key_id!r}, which "
                "KEYS.DATA_KEYS no longer holds."
            ) from None

    def seal(
        self,
        plaintext: bytes,
        context: bytes,
        *,
        connection: Connection | None = None,
        subject: str | None = None,
    ) -> str:
        """``plaintext`` sealed for ``context``, under ``subject``'s key or the current.

        Raises:
            ImproperlyConfigured: A subject without a store or a connection.
        """
        if subject is None:
            return _seal(
                self.data_key(self.current), DATA, self.current, plaintext, context
            )
        key_id, key = self.subject_key(self._connection(connection), subject)
        return _seal(key, SUBJECT, key_id, plaintext, context)

    def open(
        self, text: str, context: bytes, *, connection: Connection | None = None
    ) -> bytes | None:
        """The plaintext ``text`` sealed for ``context``; None if its subject key went.

        Raises:
            DecryptionFailed: It is not a sealed value, it was sealed for
                another context or changed, or its data key is not held.
        """
        found = sealed_key(text)
        if found is None:
            raise DecryptionFailed("The value is not one the key ring sealed.")
        kind, key_id = found
        if kind == DATA:
            key: bytes | None = self.data_key(key_id)
        else:
            key = self._subject_key_by_id(self._connection(connection), key_id)
            if key is None:
                return None
        reference, payload = text.split(":", 2)[1:]
        aead = _aesgcm()(key)
        from cryptography.exceptions import InvalidTag

        try:
            data = _unb64(payload)
            return bytes(
                aead.decrypt(
                    data[:NONCE_BYTES], data[NONCE_BYTES:], _aad(reference, context)
                )
            )
        except (ValueError, InvalidTag):
            raise DecryptionFailed(
                "The value does not open here: it was sealed for another row or "
                "column, or changed."
            ) from None

    def blind_index(self, plaintext: bytes, context: bytes) -> str:
        """``plaintext``'s blind index under the current key: ``<id>:<hex>``."""
        return self._index(self.current, plaintext, context)

    def blind_indexes(self, plaintext: bytes, context: bytes) -> tuple[str, ...]:
        """``plaintext``'s blind index under each data key: what a lookup matches."""
        return tuple(self._index(key_id, plaintext, context) for key_id in self.ids)

    def _index(self, key_id: str, plaintext: bytes, context: bytes) -> str:
        """HMAC-SHA256 under a key derived from data key ``key_id``, cut to 128 bits."""
        key = hkdf(self.data_key(key_id), info=_INDEX_INFO)
        digest = hmac.new(
            key, _INDEX_AAD + context + b"\x00" + plaintext, hashlib.sha256
        )
        return f"{key_id}:{digest.hexdigest()[:INDEX_DIGITS]}"

    def subject_key(self, connection: Connection, subject: str) -> tuple[str, bytes]:
        """The id and key of ``subject``, made and stored if it has none.

        Raises:
            ImproperlyConfigured: The ring has no subject key store.
        """
        held = _kept(connection)
        if (kept := held.get(subject)) is not None:
            return kept[1], kept[2]
        store = self._store()
        stored = store.of_subject(connection, subject)
        if stored is None:
            key = secrets.token_bytes(DATA_KEY_BYTES)
            made = self.wrap_subject_key(uuid.uuid4().hex, subject, key)
            stored = store.add(connection, made)
        key = self.unwrap_subject_key(stored)
        _hold(held, stored, key)
        return stored.id, key

    def _subject_key_by_id(self, connection: Connection, key_id: str) -> bytes | None:
        """Subject key ``key_id``, unwrapped; None once destroyed."""
        held = _kept(connection)
        if (kept := held.get(f"#{key_id}")) is not None:
            return kept[2]
        stored = self._store().find(connection, key_id)
        if stored is None:
            return None
        key = self.unwrap_subject_key(stored)
        _hold(held, stored, key)
        return key

    def wrap_subject_key(
        self, key_id: str, subject: str, key: bytes
    ) -> StoredSubjectKey:
        """``key`` of ``subject``, wrapped under the current data key."""
        nonce = secrets.token_bytes(NONCE_BYTES)
        aad = _subject_aad(key_id, subject)
        wrapped = _aesgcm()(self.data_key(self.current)).encrypt(nonce, key, aad)
        return StoredSubjectKey(key_id, subject, self.current, nonce + wrapped)

    def unwrap_subject_key(self, stored: StoredSubjectKey) -> bytes:
        """The key ``stored`` holds.

        Raises:
            DecryptionFailed: It was changed, or its data key is not held.
        """
        from cryptography.exceptions import InvalidTag

        aead = _aesgcm()(self.data_key(stored.wrapping_key))
        try:
            return bytes(
                aead.decrypt(
                    stored.wrapped[:NONCE_BYTES],
                    stored.wrapped[NONCE_BYTES:],
                    _subject_aad(stored.id, stored.subject),
                )
            )
        except InvalidTag:
            raise DecryptionFailed(
                f"The key of subject key {stored.id} was changed."
            ) from None

    def _store(self) -> SubjectKeyStore:
        """The subject key store.

        Raises:
            ImproperlyConfigured: There is none.
        """
        if self.subject_keys is None:
            raise ImproperlyConfigured(
                "Values sealed for a subject need a store of subjects' keys: "
                "install domainkit.contrib.keys."
            )
        return self.subject_keys

    @staticmethod
    def _connection(connection: Connection | None) -> Connection:
        """``connection``; subjects' keys are read on the writer's own.

        Raises:
            ImproperlyConfigured: None was given.
        """
        if connection is None:
            raise ImproperlyConfigured(
                "A value sealed for a subject is sealed and opened on a connection."
            )
        return connection


_ring: Keyring | None = None
"""The process's ring, which ``domainkit.contrib.keys`` sets up."""


def use_keyring(ring: Keyring | None) -> None:
    """Seal and open with ``ring`` from now on; None forgets the ring."""
    global _ring
    _ring = ring


def keyring() -> Keyring:
    """The process's ring.

    Raises:
        ImproperlyConfigured: None is set up: ``domainkit.contrib.keys`` is
            not installed.
    """
    if _ring is None:
        raise ImproperlyConfigured(
            "Encrypted fields need keys: install domainkit.contrib.keys, which "
            "reads them from KEYS (docs/contrib/keys.md)."
        )
    return _ring


def forget_held(subjects: Collection[str]) -> None:
    """Drop the keys of ``subjects`` from every open transaction of this process."""
    with _held_lock:
        for held in _held.values():
            for name in [
                name for name, (subject, _, _) in held.items() if subject in subjects
            ]:
                del held[name]


def _kept(connection: Connection) -> dict[str, tuple[str, str, bytes]]:
    """The subjects' keys the connection's transaction holds (none without one)."""
    transaction = connection.get_transaction()
    if transaction is None:
        return {}
    with _held_lock:
        return _held.setdefault(transaction, {})


def _hold(
    held: dict[str, tuple[str, str, bytes]], stored: StoredSubjectKey, key: bytes
) -> None:
    """Keep ``key`` in ``held``, by its subject and by its id."""
    with _held_lock:
        held[stored.subject] = held[f"#{stored.id}"] = (stored.subject, stored.id, key)


def _checked(key_id: str, key: bytes) -> bytes:
    """``key``, if it is 32 bytes.

    Raises:
        ImproperlyConfigured: It is not.
    """
    if len(key) != DATA_KEY_BYTES:
        raise ImproperlyConfigured(
            f"KEYS.DATA_KEYS[{key_id!r}] unwraps to {len(key)} bytes, not 32."
        )
    return key


def _aad(reference: str, context: bytes) -> bytes:
    """What a sealed value's tag also covers: the format, the key, the context."""
    return f"{FORMAT}\x00{reference}\x00".encode() + context


def _subject_aad(key_id: str, subject: str) -> bytes:
    """What a wrapped subject key's tag also covers: its id and subject."""
    return _SUBJECT_AAD + f"{key_id}\x00{subject}".encode("utf-8", "surrogatepass")


def _seal(key: bytes, kind: str, key_id: str, plaintext: bytes, context: bytes) -> str:
    """``plaintext`` under ``key``, for ``context``: ``dk1:<kind>.<id>:<payload>``."""
    reference = f"{kind}.{key_id}"
    nonce = secrets.token_bytes(NONCE_BYTES)
    sealed = _aesgcm()(key).encrypt(nonce, plaintext, _aad(reference, context))
    return f"{FORMAT}:{reference}:{_b64(nonce + sealed)}"
