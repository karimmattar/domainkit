"""Ledgers: write-once, read-many stores whose reads and writes are verified.

Configure one in ``LEDGERS`` with an ``immudb://`` URL (or ``memory://`` in
tests), then use it through the ``Ledger`` port::

    from domainkit.ledger import ledgers

    written = await ledgers["evidence"].append(b"key", b"value")
    entry = await ledgers["evidence"].read(b"key", revision=1)

Most projects use ``domainkit.contrib.evidence`` on top of it. See ADR 0011.
"""

from domainkit.ledger.base import (
    Ledger,
    LedgerChanged,
    LedgerEntry,
    LedgerError,
    LedgerKeyExists,
    LedgerKeyNotFound,
    LedgerTampered,
    LedgerUnavailable,
    LedgerWrite,
)
from domainkit.ledger.handler import LedgerHandler, ledgers
from domainkit.ledger.memory import MemoryLedger

__all__ = [
    "Ledger",
    "LedgerChanged",
    "LedgerEntry",
    "LedgerError",
    "LedgerHandler",
    "LedgerKeyExists",
    "LedgerKeyNotFound",
    "LedgerTampered",
    "LedgerUnavailable",
    "LedgerWrite",
    "MemoryLedger",
    "ledgers",
]
