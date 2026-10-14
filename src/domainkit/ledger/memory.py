"""An in-process ledger for tests and development (``memory://``).

It keeps every revision, like immudb, but nothing survives the process and
nothing is proven: deployments refuse it.
"""

from domainkit.ledger.base import Ledger, LedgerEntry, LedgerKeyNotFound, LedgerWrite


class MemoryLedger(Ledger):
    """Revisions in a dictionary, numbered by a transaction counter."""

    def __init__(self) -> None:
        """Start empty."""
        self._revisions: dict[bytes, list[tuple[bytes, int]]] = {}
        self._transactions = 0

    async def append(self, key: bytes, value: bytes) -> LedgerWrite:
        """Store a new revision in its own transaction."""
        self._transactions += 1
        self._revisions.setdefault(key, []).append((value, self._transactions))
        return LedgerWrite(key, self._transactions)

    async def read(self, key: bytes, revision: int | None = None) -> LedgerEntry:
        """Return the latest revision, or revision ``revision`` (1-based)."""
        revisions = self._revisions.get(key, [])
        number = len(revisions) if revision is None else revision
        if not 1 <= number <= len(revisions):
            raise LedgerKeyNotFound(f"No revision {number} of {key!r}.")
        value, transaction = revisions[number - 1]
        return LedgerEntry(key, value, transaction, number)

    async def revision_count(self, key: bytes, *, limit: int) -> int:
        """Count the revisions of ``key``, up to ``limit``."""
        return min(len(self._revisions.get(key, [])), limit)

    async def keys(self, prefix: bytes, *, limit: int) -> list[bytes]:
        """Keys with ``prefix``, sorted."""
        return sorted(key for key in self._revisions if key.startswith(prefix))[:limit]

    async def check(self) -> None:
        """Always ready."""
