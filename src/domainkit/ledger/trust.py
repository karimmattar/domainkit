"""The ledger state a client trusts, kept where a restart does not lose it.

A verified ledger (immudb) proves each answer against the last state the
client verified: a transaction number and the digest of the ledger up to
it. A client that forgets that state trusts whatever the server shows it
next, so a server rolled back to an older copy, or forked, goes unnoticed
by every client that starts afresh. The adapter keeps the state in one
place per ledger, which both its clients share (the work and the probe),
and saves it to a ``StateStore`` each time it advances; on connecting it
loads it, and refuses a server whose state is older (``LedgerRolledBack``)
or differs at the same transaction.

Without a store registered, each ledger keeps its state in memory: a
reconnect keeps it, a restart does not. ``domainkit.contrib.audit``
registers one that keeps it in ``AUDIT.DATABASE``, shared by every process
and host (``use_states``). See ADR 0011.
"""

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Final

from domainkit.apps import registry
from domainkit.ledger.base import LedgerTampered

__all__ = [
    "LedgerRolledBack",
    "MemoryStateStore",
    "StateStore",
    "TrustedState",
    "registered_states",
    "use_states",
]


class LedgerRolledBack(LedgerTampered):
    """The ledger shows an older state than one a client verified: refused."""


@dataclass(frozen=True, slots=True)
class TrustedState:
    """A ledger state a client verified: where the ledger was, and its digest."""

    database: str
    """The ledger's own name for the database (immudb's)."""
    transaction: int
    digest: bytes = field(repr=False)
    """The ledger's digest up to ``transaction`` (immudb's ``txHash``)."""
    signature: bytes = field(default=b"", repr=False)
    """The server's signature of the state, when it signs (``SERVER_SIGNING_KEY``)."""
    public_key: bytes = field(default=b"", repr=False)


class StateStore(ABC):
    """Where ledgers keep the newest state they verified, by ledger."""

    @abstractmethod
    async def load(self, ledger: str) -> TrustedState | None:
        """The state kept for ``ledger``, if any."""

    @abstractmethod
    async def save(self, ledger: str, state: TrustedState) -> None:
        """Keep ``state`` for ``ledger``, unless a later one is kept already.

        Never moves back: one client saving an older state than another
        saved keeps the later one.
        """


class MemoryStateStore(StateStore):
    """States in a dictionary: this process only (tests, and the default)."""

    def __init__(self) -> None:
        """Start empty."""
        self._states: dict[str, TrustedState] = {}
        self._lock = threading.Lock()

    async def load(self, ledger: str) -> TrustedState | None:
        """The state kept for ``ledger``."""
        with self._lock:
            return self._states.get(ledger)

    async def save(self, ledger: str, state: TrustedState) -> None:
        """Keep ``state`` unless a later one is kept."""
        with self._lock:
            kept = self._states.get(ledger)
            if kept is None or state.transaction > kept.transaction:
                self._states[ledger] = state


_registered: tuple[int, StateStore | None] = (-1, None)
"""The store an installed app registered, with the domains' generation then.

Loops on several threads share it: read and replaced with ``_lock`` held.
"""
_lock: Final = threading.Lock()


def use_states(store: StateStore) -> None:
    """Keep ledgers' trusted states in ``store``, while these domains are installed.

    Call it in ``DomainConfig.ready()``. The registration ends when the
    installed domains change (``registry.clear()``, in tests), so a store
    on a database the next settings do not have is never used.
    """
    global _registered
    with _lock:
        _registered = (registry.generation, store)


def registered_states() -> StateStore | None:
    """The store an installed app registered (``use_states``); None if none."""
    with _lock:
        generation, store = _registered
    return store if generation == registry.generation else None
