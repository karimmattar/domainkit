"""The ledger port: an append-only store whose reads and writes are verified.

Writing a key again adds a revision; earlier revisions stay readable.
``create`` writes only a new key, atomically in the ledger, so two writers
racing for one key cannot both succeed. Every adapter verifies what it
returns: immudb checks cryptographic proofs against the last state the
client trusts (see ADR 0011).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


class LedgerError(Exception):
    """Base class of ledger errors."""


class LedgerKeyNotFound(LedgerError, LookupError):
    """The key, or the requested revision of it, does not exist."""


class LedgerKeyExists(LedgerError):
    """``create`` found the key already written."""


class LedgerChanged(LedgerError):
    """``append(..., unchanged_since=...)`` found a later revision of the key."""


class LedgerTampered(LedgerError):
    """A proof or a server signature did not verify: do not trust the ledger."""


class LedgerUnavailable(LedgerError):
    """The ledger could not be reached in time."""


@dataclass(frozen=True, slots=True)
class LedgerWrite:
    """Where a verified write landed."""

    key: bytes
    transaction: int


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One verified revision of a key."""

    key: bytes
    value: bytes
    transaction: int
    revision: int
    """1 for the first write of the key, 2 for the second, and so on."""


class Ledger(ABC):
    """An append-only key-value store with verified reads and writes."""

    @abstractmethod
    async def append(
        self, key: bytes, value: bytes, *, unchanged_since: int | None = None
    ) -> LedgerWrite:
        """Write a new revision of ``key``, verified.

        With ``unchanged_since``, the transaction of the revision the caller
        read (0 for a key it found unwritten), the write happens only if no
        revision came after it; otherwise ``LedgerChanged`` is raised. The
        check and the write are one step in the ledger, so a retry, or a
        write that outlived a caller who gave up on it, lands at most once.
        """

    @abstractmethod
    async def create(self, key: bytes, value: bytes) -> LedgerWrite:
        """Write the first revision of ``key``, verified.

        Raises ``LedgerKeyExists`` if the key has one: the check and the write
        are one step in the ledger.
        """

    @abstractmethod
    async def read(self, key: bytes, revision: int | None = None) -> LedgerEntry:
        """Return the latest (or the given) revision of ``key``, verified.

        Raises ``LedgerKeyNotFound`` when it does not exist.
        """

    @abstractmethod
    async def revision_count(self, key: bytes, *, limit: int) -> int:
        """How many revisions ``key`` has, counting up to ``limit``."""

    @abstractmethod
    async def keys(self, prefix: bytes, *, limit: int) -> list[bytes]:
        """Keys that start with ``prefix``, in key order, at most ``limit``."""

    @abstractmethod
    async def check(self) -> None:
        """Raise when the ledger cannot serve requests (readiness probe)."""

    async def close(self) -> None:
        """Release clients and threads; the ledger reconnects on next use."""
        return  # Nothing to release by default.
